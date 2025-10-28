# Nodriver Project Documentation

## Project Overview

**nodriver** is a Python async browser automation library using Chrome DevTools Protocol (CDP). It is the official successor to [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver/).

### Core Mission
- Provide undetectable browser automation
- Bypass anti-bot detection systems
- Offer a clean, async/await-based API
- Support headless and headful operation

### Architecture

#### Key Components

1. **Browser** (`nodriver/core/browser.py`)
   - Manages Chrome/Chromium process lifecycle
   - Handles browser-level configuration and settings
   - Tracks tabs/targets
   - Uses `_update_targets_lock` for thread-safe target list modifications

2. **Tab/Connection** (`nodriver/core/tab.py`, `nodriver/core/connection.py`)
   - `Connection`: Base class handling WebSocket communication with CDP
   - `Tab`: Extends Connection, represents a browser tab/page
   - Event-driven architecture with handler registration
   - Async message passing via WebSocket

3. **Element** (`nodriver/core/element.py`)
   - Represents DOM elements
   - Provides interaction methods (click, type, etc.)
   - Position calculation and visibility checks

4. **Utilities** (`nodriver/core/util.py`)
   - ProxyForwarder for SOCKS5 proxy support
   - Port management
   - Browser instance registry
   - Cleanup functions

### Threading & Concurrency

- Heavily uses `asyncio` for all I/O operations
- Critical sections protected by locks:
  - `Browser._update_targets_lock` - protects `self.targets` list
  - `Connection._connection_lock` - protects WebSocket connection state
  - `Connection.mapper` - protected for concurrent transaction access

### CDP Communication Pattern

```
Browser -> Connection -> WebSocket -> Chrome DevTools Protocol
                |
                +-> Transaction (Future-based request/response)
                +-> Event Handlers (callback-based events)
```

## Known Issues & Technical Debt

### Critical Issues
None currently known.

### Minor Issues

1. **Bare Exception Handlers** (Low Priority)
   - `connection.py:106` - `has_exception()` catches all exceptions
   - `connection.py:160` - `EventTransaction.__init__()` silently swallows parent init failures
   - `tab.py:1747` - File cleanup catches all exceptions

   These are generally defensive but could hide bugs. Consider catching specific exceptions.

2. **Broad Exception Catches**
   - Several `except (Exception,)` clauses that log but continue
   - Most are intentional for robustness, but review if errors seem hidden

3. **Comment-Indicated Issues**
   - `connection.py:430` - Intentionally broad exception catch with comment "as broad as possible"
   - No other TODO/FIXME/HACK comments found

### Architecture Notes

- Handler registration in `Connection._register_handlers()` is complex and intentionally permissive
- Target updates now use proper locking (previously had race conditions)
- Browser stop() now has proper timeouts (5s for terminate, 3s for kill)

## Fork Analysis & Status

### Reviewed Forks

#### 1. **isaiah-rps/nodriver** (Most Comprehensive)
**Status:** Active (Sep 2025)
**Quality:** Production-grade improvements
**Key Features:**
- Browser context support (`browser_context_id`)
- Enhanced tab attributes (`.url`, `.target_id`, `.browser_context_id`)
- Click count parameter for double/triple clicks
- Improved `flash_point()` accuracy
- Race condition fixes in transaction completion
- ProxyForwarder exposed on Tab class
- Session ID support

**Applied to This Fork:**
- ✅ Race condition fixes (transaction completion, domain removal)
- ✅ `click_count` parameter
- ✅ Improved `flash_point()` positioning
- ✅ `tab.url` property
- ❌ Browser context support (not needed yet)
- ❌ ProxyForwarder on Tab (not needed yet)

#### 2. **Connor9994/nodriver** (Most Popular - 20★)
**Status:** Last updated Aug 2024
**Key Features:**
- `mouse_move_random()` - random position within element bounds
- `send_keys_random()` - variable delay between keystrokes
- Anti-detection through human-like behavior

**Applied to This Fork:**
- ✅ Both randomization methods

#### 3. **max32002/nodriver**
**Status:** Active (Oct 2025)
**Key Features:**
- Shadow root workaround (fallback document retrieval)
- Cookie access edge case handling

**Applied to This Fork:**
- ❌ Not applied yet (low priority, defensive programming)

#### 4. **tcortega/nodriver**
**Status:** Active (Oct 2025) - Feature branch only
**Key Features:**
- HTTP/HTTPS proxy authentication with username/password
- Custom SSL context support

**Applied to This Fork:**
- ❌ Not needed (only if authenticated proxies required)

#### 5. **boludoz/nodriver**
**Status:** Active (Sep 2025)
**Key Features:**
- Selenium-compatible Element methods
- `box_model()`, `size()`, `location()`, `rect()`
- `is_displayed()`, `is_enabled()`, `is_selected()`, `is_clickable()`

**Applied to This Fork:**
- ❌ Not needed (nice-to-have for Selenium migration)

### Fork Recommendations

**If you need:**
- **Browser isolation/multi-session:** Use isaiah-rps browser context support
- **Authenticated proxies:** Use tcortega proxy auth branch
- **Selenium compatibility:** Use boludoz element methods
- **Shadow DOM edge cases:** Use max32002 workaround

## Recent Improvements (This Fork)

### Race Condition Fixes
1. Connection handler domain removal (concurrent `_register_handlers()` calls)
2. Transaction completion (duplicate replies causing `InvalidStateError`)
3. Target list modifications (proper locking with `_update_targets_lock`)
4. Browser stop() method (concurrent access during shutdown)

### Browser Lifecycle
- `browser.stop()` simplified with proper timeouts
- Registry cleanup to allow garbage collection
- Removed pointless retry loops
- Catch only expected exceptions (fail fast on bugs)

### Async Task Handling
- Background tasks now tracked with `add_done_callback()`
- Uses `get_running_loop()` instead of deprecated `get_event_loop()`
- Exceptions properly logged instead of silently lost

### User Features
- `tab.url` property for direct URL access
- `click_count` parameter for double/triple clicks
- Improved `flash_point()` accuracy
- `mouse_move_random()` and `send_keys_random()` for anti-detection

## Development Guidelines

### Exception Handling Philosophy
Per user's coding guidelines:
- **Fail fast** - avoid catching exceptions unless necessary
- **Fatal errors** - programming/logic errors should crash, not be hidden
- **Expected exceptions only** - catch specific exceptions, not broad `Exception`
- **No error message wrapping** - tracebacks are useful, don't obscure them

### Code Style
- Follow PEP 8 with E305/E306 (blank lines)
- DRY and YAGNI principles
- Comments for clarity, not obvious explanations
- No historical/refactoring notes in comments
- Never include attribution in commit messages

### Concurrency Patterns
- Use locks for shared state (`self.targets`, `self.mapper`)
- Track async tasks with `add_done_callback()` for exception handling
- Use `asyncio.wait()` with proper task management (cancel pending)
- Avoid fire-and-forget `create_task()` without tracking

## Testing Considerations

### Areas to Test
1. **Concurrent tab creation/destruction** - Stresses target list locking
2. **Browser stop during active operations** - Tests cleanup robustness
3. **Handler registration/removal during events** - Tests domain management
4. **Multiple simultaneous CDP commands** - Tests transaction handling
5. **Proxy forwarding under load** - Tests socket handling

### Known Edge Cases
- Shadow DOM elements may fail document queries (workaround available in max32002 fork)
- Cookie-protected pages may need special handling
- Browser crash during operations may leave orphaned processes
- Proxy connections may fail silently during shutdown (expected, logged at debug level)

## Future Considerations

### Potential Improvements
1. Consider applying max32002's shadow root workaround if users report issues
2. Add browser context support if multi-session isolation needed
3. Implement proxy authentication if enterprise use case emerges
4. Add Selenium-compatible methods if migration users request them

### Monitoring
Watch upstream (ultrafunkamsterdam/nodriver) for:
- CDP protocol updates
- New anti-detection techniques
- Browser compatibility changes
- Security fixes

Watch active forks for:
- Bug fixes that haven't been submitted upstream
- Feature additions that gain traction
- Race conditions or stability improvements

## References

- **Upstream:** https://github.com/ultrafunkamsterdam/nodriver
- **Chrome DevTools Protocol:** https://chromedevtools.github.io/devtools-protocol/
- **Active Forks:**
  - isaiah-rps: https://github.com/isaiah-rps/nodriver
  - Connor9994: https://github.com/Connor9994/nodriver
  - max32002: https://github.com/max32002/nodriver
  - tcortega: https://github.com/tcortega/nodriver
  - boludoz: https://github.com/boludoz/nodriver
