# Known Issues

## nodriver/core/connection.py

### Critical Issues

#### 1. Unhandled KeyError crash in _listener() (line 476)

**Location**: `connection.py:476`

```python
tx: Transaction = self.mapper.pop(message["id"])
```

**Problem**: If Chrome sends duplicate response or unsolicited message, KeyError crashes the entire websocket listener task.

**Impact**: Terminates all CDP communication permanently.

**Fix**: Wrap in try/except:
```python
try:
    tx: Transaction = self.mapper.pop(message["id"])
except KeyError:
    logger.warning("Received message with unknown id: %s", message.get("id"))
    continue
```

---

#### 2. Race condition in Transaction.__call__() (line 124)

**Location**: `connection.py:119-124`

```python
if self.done():
    return

if "error" in response:
    return self.set_exception(ProtocolException(response["error"]))
```

**Problem**: Between the `done()` check and `set_exception()` call, another coroutine could complete the future, causing `InvalidStateError`.

**Impact**: Crashes transaction handling if duplicate responses arrive.

**Fix**: Protect like `set_result()` at line 133-136:
```python
if "error" in response:
    try:
        self.set_exception(ProtocolException(response["error"]))
    except asyncio.InvalidStateError:
        pass
    return
```

---

#### 3. Websockets API mismatch (line 335)

**Location**: `connection.py:19, 205-206, 335`

```python
import websockets.asyncio.client  # line 19
def websocket(self) -> websockets.asyncio.client.ClientConnection:  # line 205

self._websocket = await websockets.connect(...)  # line 335 - WRONG API
```

**Problem**: Type hint says new API (`websockets.asyncio.client.ClientConnection`), code uses old deprecated API (`websockets.connect`).

**Impact**: Type checking fails, potential runtime incompatibility, deprecation warnings.

**Fix**:
```python
self._websocket = await websockets.asyncio.client.connect(
    self.websocket_url,
    ping_timeout=PING_TIMEOUT,
    max_size=MAX_SIZE,
)
```

---

### High Priority Issues

#### 4. Memory leak: Orphaned transactions (timeout) (line 553)

**Location**: `connection.py:549-553`

```python
tx.id = the_id
self.mapper[the_id] = tx
await self.websocket.send(tx.message)
return await tx  # if this times out or Chrome never responds, stays in mapper forever
```

**Problem**: If Chrome crashes, drops connection, or never responds, Transaction stays in `self.mapper` indefinitely.

**Impact**: Long-running sessions leak memory on any CDP timeout/failure.

**Fix**: Add timeout wrapper in `send()`:
```python
try:
    return await asyncio.wait_for(tx, timeout=30.0)
except asyncio.TimeoutError:
    async with self._lock:
        self.mapper.pop(the_id, None)
    raise
```

---

#### 5. Memory leak: Orphaned transactions (send failure) (line 552)

**Location**: `connection.py:547-553`

```python
async with self._lock:
    self.mapper[the_id] = tx  # mapped
await self.websocket.send(tx.message)  # if this raises, tx stays in mapper
return await tx
```

**Problem**: Transaction mapped inside lock, but send happens outside. If send fails, cleanup never happens.

**Impact**: Every websocket send failure leaks a transaction.

**Fix**: Wrap websocket send in try/except and clean up on failure:
```python
try:
    await self.websocket.send(tx.message)
except Exception:
    async with self._lock:
        self.mapper.pop(the_id, None)
    raise
```

---

#### 6. Race condition: handlers dictionary (lines 287, 305, 321, 324, 407-408, 494-500)

**Problem**: `self.handlers` modified from multiple contexts without locking:
- User calling `add_handler()` / `remove_handler()`
- `_register_handlers()` during send()
- `_listener()` reading during events

**Specific TOCTOU bug at line 407-408**:
```python
if len(self.handlers[event_type]) == 0:  # CHECK
    self.handlers.pop(event_type)         # USE - could pop non-empty list if handler added between
```

**Concurrent modification at line 305**: Removing handler from list while another coroutine might be iterating.

**Impact**: Could remove handlers that were just added, or crash on concurrent modification.

**Fix**: Add `_handlers_lock` or use thread-safe dict operations.

---

#### 7. Race condition: enabled_domains duplicates (lines 418-426)

```python
elif domain_mod not in self.enabled_domains:  # line 418 - CHECK
    # ...
    self.enabled_domains.append(domain_mod)   # line 426 - USE
```

**Problem**: Concurrent `_register_handlers()` calls both see domain not present, both append → duplicates.

**Impact**: Duplicate enable calls to Chrome, list corruption.

**Fix**: Check-and-add atomically inside lock, or use a set.

---

#### 8. Race condition: websocket state check (lines 235-238, 543)

```python
@property
def closed(self):
    if not self.websocket:  # UNPROTECTED READ
        return True
    return bool(self.websocket.close_code)

# Used at line 543:
if self.closed:
    await self.connect()
```

**Problem**: `connect()` modifies `self._websocket` inside `_connection_lock`, but `closed` reads it without lock.

**Impact**: Race between checking closed state and connection/disconnection.

**Fix**: Either check inside lock or make `closed` property acquire lock.

---

### Medium Priority Issues

#### 9. Inefficient: Sleep after timeout (lines 463-465)

```python
except asyncio.TimeoutError as e:
    await asyncio.sleep(0.05)  # why sleep AFTER a 0.05s timeout?
    continue
```

**Problem**: `recv()` already waited 0.05s, then we wait another 0.05s. Doubles latency for no reason.

**Fix**: Just `continue` without sleep.

---

#### 10. Auto-reconnect fights explicit disconnect (lines 543-544)

```python
if self.closed:
    await self.connect()
```

**Problem**: After user calls `disconnect()`, any `send()` immediately reconnects. No way to stay disconnected.

**Impact**: May not be a bug depending on intended behavior, but seems questionable.

**Fix**: Add explicit `_disconnected_by_user` flag to prevent auto-reconnect.

---

#### 11. Bare except in has_exception (line 106)

```python
except:
    return True
```

**Problem**: Claims transaction has exception for ANY error, not just `InvalidStateError`.

**Impact**: Could misreport transaction state on bugs like `AttributeError`, `TypeError`, etc.

**Fix**: Catch only `Exception` (not `BaseException`) or specific exceptions.

---

## nodriver/core/tab.py

### High Priority Issues

#### 1. Race condition in _prepare_headless and _prepare_expert (lines 214, 234)

**Location**: `tab.py:212-231, 233-250`

```python
async def _prepare_headless(self):
    if getattr(self, "_prep_headless_done", None):  # CHECK
        return
    # ... do work ...
    setattr(self, "_prep_headless_done", True)  # SET

async def _prepare_expert(self):
    if getattr(self, "_prep_expert_done", None):  # CHECK
        return
    # ... do work ...
    setattr(self, "_prep_expert_done", True)  # SET
```

**Problem**: TOCTOU bug - multiple concurrent calls could all pass the check before any sets the flag, resulting in duplicate preparation work.

**Impact**: Multiple concurrent calls execute preparation logic simultaneously, potentially causing duplicate CDP calls or race conditions in initialization.

**Fix**: Use proper async locks:
```python
async def _prepare_headless(self):
    if not hasattr(self, '_prep_headless_lock'):
        self._prep_headless_lock = asyncio.Lock()

    async with self._prep_headless_lock:
        if getattr(self, "_prep_headless_done", None):
            return
        # ... do work ...
        setattr(self, "_prep_headless_done", True)
```

---

### Low Priority Issues

#### 2. Bare exception handlers (lines 645, 729, 1747, 1754)

**Location**: Multiple locations in `tab.py`

**Lines 645, 729**: Element creation failures
```python
try:
    elem = element.create(node, self, doc)
except:  # noqa
    continue
```

**Lines 1747, 1754**: File cleanup
```python
try:
    os.unlink("screen.jpg")
except:
    logger.warning("could not unlink temporary screenshot")
```

**Problem**: Bare `except:` catches all exceptions including `SystemExit`, `KeyboardInterrupt`, and masks bugs like `AttributeError` or `TypeError`.

**Impact**: Could hide bugs or make debugging difficult. Generally defensive but not best practice.

**Fix**: Catch specific exceptions:
```python
except Exception:  # don't catch BaseException (KeyboardInterrupt, SystemExit)
    continue
```

---

## nodriver/core/connection.py (Additional)

### Low Priority Issues

#### 12. Bare exception in EventTransaction.__init__ (line 160)

**Location**: `connection.py:157-163`

```python
def __init__(self, event_object):
    try:
        super().__init__(None)
    except:
        pass
    self.set_result(event_object)
    self.event = self.value = self.result()
```

**Problem**: Silently swallows all exceptions from parent `__init__()`, including bugs.

**Impact**: Hides initialization errors, makes debugging difficult.

**Fix**: Catch specific expected exception or log the error:
```python
try:
    super().__init__(None)
except TypeError:  # expected when passing None to Transaction.__init__
    pass
```

---

## nodriver/core/util.py

### Critical Issues

#### 1. SOCKS proxy protocol parsing bug (line 4651-4658)

**Location**: `util.py:4651-4658` inside `ProxyForwarder.handle_socks_request()`

```python
async def read(fmt):
    """
    Read from the byte stream
    :param str fmt: struct format specifier
    :return tuple:
    """
    data = await reader.read(calcsize(fmt))
    return unpack(fmt, data)
```

**Problem**: `StreamReader.read(n)` can return **fewer than n bytes** if the stream ends or data arrives in chunks. The code assumes it always receives exactly `calcsize(fmt)` bytes, then blindly unpacks it. This causes `struct.error: unpack requires a buffer of X bytes` when partial data is received.

**Impact**:
- SOCKS proxy with authentication fails randomly (on slow connections, packet boundaries)
- Malformed struct unpack crashes the proxy forwarder
- Non-deterministic failures make debugging difficult

**Fix**: Use `readexactly()` instead of `read()`:
```python
async def read(fmt):
    data = await reader.readexactly(calcsize(fmt))
    return unpack(fmt, data)
```

`readexactly()` guarantees to return exactly N bytes or raise `IncompleteReadError` if the stream ends prematurely.

**Affected lines**: 4660, 4661, 4668, 4672, 4676, 4681, 4683, 4699, 4714 - all calls to `read()` helper.

---
