# SIYI A8 Mini AI tracking: firmware inspection

Inspected October 4, 2026. This is a static analysis of the firmware in Downloads, not a hardware test. The camera and gimbal binaries were unpacked and disassembled; no firmware was executed or flashed.

The useful discovery is that **the A8 Mini firmware already receives tracking coordinates and runs the gimbal-centering controller**. SIYI's separate AI module supplies the visual recognition/tracking results. The camera firmware contains a separate TCP interface for receiving those results. This SDK now implements that route as an experimental fixed-scene point-lock controller. Its protocol and lifecycle have offline checks; physical tracking remains unverified.

## Firmware inspected

Source directory: `C:\Users\nickt\Downloads\A8_mini_Firmware_Pack_v0_4_9_db70c2fb (2)`.

| Image | Size | SHA-256 |
| --- | ---: | --- |
| Camera v0.3.7 svn3085, July 7, 2026: `SIYI_4K_MINI_UpgradeSD.bin` | 11,943,960 bytes | `f0fe8031784c3d623be3b90424510a655ede79be595d9eaa68072213f2dfa60a` |
| Gimbal v0.4.9 svn10601, July 7, 2026 | 255,948 bytes | `963c961f3bc8fdc0828fe3910257dde7c3486dc896508dc8e0add3ccdc2d8c30` |

The installation script identifies an XZ-compressed CPIO root filesystem and JFFS2 `miservice` and `customer` partitions. I decoded 549 rootfs entries, four miservice file entries, and 161 customer file entries. Every parsed JFFS2 header, directory-entry, and inode/data CRC validated: **zero CRC errors**. Rootfs entry counts include directories and links; they are not all executable files.

The startup script runs `/customer/bin/cardv`, a 32-bit ARM Linux executable with exported function names. These names, its command tables, and the actual instructions provide the strongest camera-side evidence. The gimbal image is Cortex-M Thumb code; its reset vector maps to the stub at file offset `0x1c0`, giving a flash base of `0x08003800`.

Evidence: [extraction script](../.tools/firmware-analysis/inspect_firmware.py), [manifest](../.tools/firmware-analysis/manifest.json), [camera disassembler](../.tools/firmware-analysis/disassemble.py), [gimbal analysis script](../.tools/firmware-analysis/gimbal.py).

## What happens during tracking

```text
Camera video → separate SIYI AI tracking module
                  ↓ target coordinates / box / status
           A8 camera's tracking-data handler
                  ↓ internal gimbal command 0x36
           Gimbal coordinate storage and control loop
                  ↓ horizontal / vertical corrections
           Camera turns to keep the target centered

           Optional copy of target data → external SDK clients
```

The separate-module architecture is also documented by [SIYI's A8 Mini product page](https://www.siyi.biz/en/product/tri-axis-single-camera-gimbal/a8-mini/). The local camera protocol explicitly says the camera cannot enable AI mode by itself and needs an AI module. This does not mean the camera lacks tracking-related commands: its firmware has both a mode flag and a target-data input handler.

### 1. AI mode and the target-data input are on a separate camera interface

`tcp_server_init` at camera address `0x000651bc` passes `0x9188` (decimal **37256**) to `tcp_server_create`. The external SDK TCP initializer instead passes `0x918c` (**37260**). The image's network configuration sets the camera IP to `192.168.144.25`.

The separate interface's table, selected by `get_tcp_camera_action_func`, contains:

| Command | Table address | Wrapper | Actual handler |
| --- | --- | --- | --- |
| `0xA2` | `0x000e3a40` | `0x0006552c` | `camera_sdk_get_ai_action` |
| `0xA3` | `0x000e3a48` | `0x000654e4` | `camera_sdk_set_ai_action` |
| `0xAB` | `0x000e3a50` | `0x000654d8` | `camera_sdk_set_ai_track_target_info_action` |

The `0xA3` handler checks for a one-byte payload. A value of `1` sets its AI-mode flag and sends internal gimbal motion-mode command `(0x6B, 0x15)` with value `3`; disabling clears the flag and includes a path to restore the previous mode. Setting this flag is not image recognition.

**These commands belong to the separate camera protocol.** They use the long framing described below on port 37256, rather than ordinary public SDK packets on port 37260. Live compatibility remains unverified.

Evidence: [port 37256 initializer](../.tools/firmware-analysis/disassembly/tcp_server_init.txt), [port 37260 initializer](../.tools/firmware-analysis/disassembly/tcp_sdk_server_init.txt), [command tables](../.tools/firmware-analysis/command-tables.txt), [wrappers](../.tools/firmware-analysis/disassembly/range-654bc.txt), [mode handler](../.tools/firmware-analysis/disassembly/camera_sdk_set_ai_action.txt).

### 2. The camera forwards the target payload to the gimbal

`camera_sdk_set_ai_track_target_info_action` at `0x00062c0c`:

- Reads the AI-mode flag and requires it to equal `1`.
- If the mode is disabled, returns a one-byte status value `2` without forwarding the target.
- If enabled, calls `protocol_send_gimbal(0x6B, 0x36, payload, payload_length, 0)`.
- If a client's coordinate-stream flag is enabled, sends the same payload to that client as public command `0x50`. It checks UART, UDP, and TCP flags separately.

This handler forwards coordinates; it does not decode video or run a detector. The exact identity of the sender is not enforced by the instructions inspected here. That alone does not prove a replacement sender will work: the surrounding transport and parser still matter.

Evidence: [target-data handler](../.tools/firmware-analysis/disassembly/camera_sdk_set_ai_track_target_info_action.txt), [internal send function](../.tools/firmware-analysis/disassembly/protocol_send_gimbal.txt).

### 3. The gimbal works in image coordinates and runs its own controller

At gimbal address `0x080337f4`, a target-data routine copies ten bytes into RAM at `0x2000e09c`, marks new data available, and multiplies four unsigned 16-bit fields by **0.5**, storing the resulting coordinates/box dimensions at `0x2000253e` through `0x20002544`. Two wrappers call this routine at `0x08021518` and `0x08021cd0`. The complete gimbal command dispatch mapping has not been reconstructed, so the association with the camera's internal `0x36` route is a strong inference from the matching payload and surrounding code.

The local vendor protocol describes the ten-byte stream payload as:

```text
uint16 x, y, width, height
uint8  target_type, tracking_status
```

Coordinates describe the box center in a **1280×720** image. The gimbal halves these values, then uses a **640×360** image center. At `0x0800b938`, the control routine computes errors equivalent to:

```text
horizontal_error = x / 2 - 320
vertical_error   = 180 - y / 2
```

It calls the controller routine at `0x08014c54` for each axis. That routine contains proportional, accumulated-integral, and error-difference terms, plus limits: it is a PID-style controller. This finding identifies the structure, not a complete gain/timing specification.

The steering conversion routine at `0x0800e2a0` also reads these coordinates, camera orientation, and a zoom-like parameter clamped to 10–60. **Interpreting that parameter as tenths of 1×–6× zoom is an inference**, consistent with the camera's digital-zoom convention. It transforms controller outputs before calling the gimbal motion routine. Thus the path accounts for more than a raw pixel error.

Other branches use target-box dimensions, missing/new-data flags, counters, and tracking status. I have not established the loop's exact rate, timeout in milliseconds, all loss/recovery behavior, or every control mode. Numeric counters alone are insufficient to establish wall-clock time.

Evidence: [target-data routine](../.tools/firmware-analysis/disassembly/gimbal-range-80337c0.txt), [centering routine](../.tools/firmware-analysis/disassembly/gimbal-range-800b900.txt), [controller arithmetic](../.tools/firmware-analysis/disassembly/gimbal-range-8014c54.txt), [coordinate/steering conversion](../.tools/firmware-analysis/disassembly/gimbal-range-800e2a0.txt).

### 4. External clients can receive tracking coordinates

The public UDP, TCP, and UART SDK command tables confirm the following commands in this camera build:

| Command | Function |
| --- | --- |
| `0x4D` | Read AI-mode status |
| `0x4E` | Read tracking-coordinate stream status |
| `0x51` | Enable/disable coordinate output for the current transport |
| `0x50` | Outgoing target payload, copied by the target-data handler |

The vendor protocol lists normal AI tracking, temporary loss, loss, cancellation, and normal any-object tracking states. It says coordinate output follows the video frame rate. Those state meanings and rate are documentation claims; static analysis here confirms the routes and payload forwarding, not actual emission timing.

The protocol's `0x50` section incorrectly refers to `0x4F` as the command enabling output, while its own setter section uses `0x51`. **This firmware's command tables route `0x51` to the setter**, resolving that specific documentation inconsistency.

Evidence: [command tables](../.tools/firmware-analysis/command-tables.txt), [stream setter](../.tools/firmware-analysis/disassembly/camera_sdk_set_ai_target_info_stream_sta_action.txt), and [checked-in vendor protocol](../SIYI_SDK_PROTOCOL.txt) around lines 3140–3290.

## What this means for our SDK

Select **Settings → Point lock steering → SIYI firmware (experimental)** and save, then use the existing point-lock gesture. Angle targets remain the default. The dashboard accepts firmware mode only when the camera reports camera 0.3.7 and gimbal 0.4.9, the versions inspected here. Version numbers do not authenticate a firmware binary or prove compatibility with another model.

`FirmwarePointLock` reuses the existing `PointLock` optical-flow tracker for a fixed scene point. It scales the current decoded image's point and marker box to 1280×720, without angle conversion, prediction, or another zoom correction. It uses a separate `FirmwareTrackingClient`; the existing public SDK connection remains available for telemetry and manual zoom. This does not add moving-object detection.

The new controller confirms enable with a setter reply and a separate mode query before sending targets. Each fresh processed frame supplies at most one target update. Confidence below 0.2, an off-screen point, changed dimensions, a failed mode check, or a broken private connection releases immediately. A watchdog checks every 25 ms and initiates release after 400 ms without a fresh frame. Operations are bounded to 400 ms; releasing is not a guarantee of a physical stop within 400 ms, especially when the network has failed.

Manual steering, centering, mode changes, calibration, changed settings, disconnect, and shutdown disable AI before handing control back. If exit cannot be confirmed, a public zero-speed stop is attempted and the dashboard reports the uncertainty and blocks further steering/locks. There is no automatic reconnection, fallback, or lock restoration. After an unconfirmed exit, verify the camera has stopped and reset its tracking state before restarting the dashboard. The firmware's own stale-target timeout remains unknown.

### Recovered TCP wire format

Camera `long_protocol_send` at `0x5ff78`, `long_prot_parse_char` at `0x600c4`, and the link record at `0xe5108` establish the framing and port's parser. The record points to `tcp_camera_link_read`, `tcp_camera_link_write`, and `get_tcp_camera_action_func`, with a 4096-byte receive buffer.

| Byte offset | Field |
| --- | --- |
| 0–3 | Magic `55 66 AA BB` |
| 4 | Flags: bit 0 requests reply, bit 1 marks reply; other bits must be zero |
| 5–8 | Payload length, little-endian uint32 |
| 9–10 | Sender sequence, little-endian uint16 |
| 11 | Command byte |
| 12–15 | CRC32 of bytes 0–11, stored little-endian |
| 16 onward | Payload |
| 16 + payload length | CRC32 of header including header CRC and payload, little-endian uint32 |

`crc_check_32bites` at `0x68868` calls `CRC32_cal` at `0x4f0ac` with seed zero. It is MSB-first, polynomial `0x04C11DB7`, no final XOR, using the table at `0xb94e0`. It is **not** the usual reflected `zlib.crc32`. The check value for ASCII `123456789` is `0x89a1897f`.

Mode query `A2` has an empty request and one-byte 0/1 reply. Mode setter `A3` takes one byte 0/1 and returns the resulting mode. The firmware allocates a new outgoing sequence, so replies are matched by command and expected mode under serialized requests. Target `AB` uses the ten-byte payload documented above. Successful target forwarding has no private success ACK; a one-byte `02` reply means AI mode was disabled. The client monitors mode independently and never interprets a completed TCP write as proof of gimbal movement.

Reference packets (computed independently with the firmware's CRC table, **not hardware captures**):

```text
Query, seq 1:   5566aabb01000000000100a224b603a4a04dda26
Enable, seq 2:  5566aabb01010000000200a30b36fced01da2aa71d
Target, seq 3:  5566aabb000a0000000300ab2b684d33800268013c003c00ff04d178d6ab
Disable, seq 4: 5566aabb01010000000400a319dd2fe900bd9e9011
```

The target is centre (640,360), box (60,60), type 255 and state 4. Target packets request no ACK. See [encoder/client](../siyi_sdk/firmware_tracking.py) and [three focused tests](../tests/tracking/test_firmware.py).

### Why state 4 avoids automatic zoom

At gimbal `0x0800bf60`, target byte 9 equal to 4 forces RAM `0x20001e08` to 1500. Execution continues into pointing conversion `0x0800e2a0` and the motor command at `0x0800bf8c`; this branch does not disable pan/tilt tracking.

The consumer at `0x08005ae0` compares that RAM value to 1510 and 1490 and sends +1, -1, or 0 through internal command `(0x6b,0x3c)`. Thus 1500 produces zero. The camera dispatch table at `0xe3cf0` maps that command to `gimbal_ai_zoom_action` at `0x63fd4`, whose nonzero branches start ISP zoom and zero branch pauses a preceding AI zoom. This completes the static link from state 4 to neutral automatic zoom. Manual zoom behavior still needs physical verification.

Evidence retained locally: [long-protocol encoder and CRC wrapper](../.tools/firmware-analysis/encode-wire.txt), [parser](../.tools/firmware-analysis/long-wire.txt), [CRC and mode handlers](../.tools/firmware-analysis/crc-mode.txt), [zoom output consumer](../.tools/firmware-analysis/output-consumers.txt), and [camera zoom handler](../.tools/firmware-analysis/zoom-wire.txt). These extracted analysis artifacts are ignored by Git; addresses and source-image hashes above provide the reproducible reference.

### Validation boundary

The three new automated tests cover reference packets and corrupt/split/coalesced reads; image scaling, loss and lack of app steering; and enable confirmation, release during an in-flight update, missing replies, stale video, connection loss, incompatible versions, settings, manual mode takeover, and shutdown. The combined tracking/pointing run passed 58 tests; after the version guard and release-race case, the affected subset passed 13 tests. Ruff, Black, targeted mypy, Python compilation, JavaScript syntax, and diff whitespace checks passed. The broader dashboard run encountered a missing `httpx`/`httpx2` dependency in an existing command-catalog test; that test is not reported as passing.

UI checks used Playwright with installed Chrome at `http://127.0.0.1:8093`, with camera startup disabled, at 1440×1000 and 390×844. Browser plugin was not available. Selecting firmware disabled app tuning; Save Changes reached the server; reload retained the selection; selecting angle restored tuning. The settings modal was made scrollable after this check exposed an unreachable Save button. No JavaScript runtime exceptions occurred. Network restrictions blocked the existing external Google Fonts/Font Awesome stylesheets, and the existing favicon URL returned 404; these asset errors are not attributed to tracking. The temporary QA server was stopped afterward.

A read-only connection attempt to `192.168.144.25:37256` did not connect, so no physical movement was commanded or observed.

Remaining stationary-camera check: use a textured stationary scene, start with a point near centre, verify centering and stable zoom, change manual zoom, then release and take manual control. Repeat with video and private TCP interrupted; verify actual motor stop and reported failure. Do not infer these results from the offline tests.

The actual SIYI recognition model and its training/tracking algorithm are not established by the A8 firmware. No common model filenames such as `.onnx`, `.rknn`, or `.tflite` appeared in the decoded file inventory, but that does not rule out embedded or differently packaged models. Libraries named `libmi_ai.so` are not proof of artificial intelligence: the executable imports audio-input functions such as `MI_AI_SetVqeVolume` from that library. It also includes generic ADAS and image-processing libraries; their presence does not prove the selected-object tracker runs locally.

Only A8 firmware packs were present among matching Downloads filenames; no separate SIYI AI-module firmware image was found. Inspecting that module's firmware would be the next source-based step for identifying its detector/tracker internals.
