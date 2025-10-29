# Known Issues

## nodriver/core/connection.py

### High Priority Issues

#### 1. Memory leak: Orphaned transactions (timeout)

**Location**: `connection.py` in `send()` method

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

#### 2. Memory leak: Orphaned transactions (send failure)

**Location**: `connection.py` in `send()` method

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

### Medium Priority Issues

#### 4. Auto-reconnect fights explicit disconnect

**Location**: `connection.py` in `send()` method

```python
if self.closed:
    await self.connect()
```

**Problem**: After user calls `disconnect()`, any `send()` immediately reconnects. No way to stay disconnected.

**Impact**: May not be a bug depending on intended behavior, but seems questionable.

**Fix**: Add explicit `_disconnected_by_user` flag to prevent auto-reconnect.

---

### Low Priority Issues

#### 5. Bare except in has_exception

**Location**: `connection.py` in `Transaction.has_exception` property

```python
except:
    return True
```

**Problem**: Claims transaction has exception for ANY error, not just `InvalidStateError`.

**Impact**: Could misreport transaction state on bugs like `AttributeError`, `TypeError`, etc.

**Fix**: Catch only `Exception` (not `BaseException`) or specific exceptions.

---

#### 6. Bare exception in EventTransaction.__init__

**Location**: `connection.py` in `EventTransaction.__init__()`

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

## nodriver/core/tab.py

### Low Priority Issues

#### 2. Bare exception handlers

**Location**: Multiple locations in `tab.py`

**Element creation failures**:
```python
try:
    elem = element.create(node, self, doc)
except:  # noqa
    continue
```

**File cleanup**:
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

## nodriver/core/util.py

### Critical Issues

#### 1. SOCKS proxy protocol parsing bug

**Location**: `util.py` inside `ProxyForwarder.handle_socks_request()`

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

---
