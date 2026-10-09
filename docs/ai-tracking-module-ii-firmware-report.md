# SIYI AI Tracking Module II v1.1.0: what it does and how it works

Inspected October 5, 2026. Source: the firmware package dated February 7, 2026 supplied in Downloads.

## Main finding

**This module is the video-analysis computer in SIYI's tracking system.** It receives compressed camera video, decodes the pictures, recognizes objects with packaged YOLOv6 neural networks, follows a selected object or image region, and sends its position back to the camera. It also produces processed video for viewing. The camera and gimbal perform the physical steering.

There are two distinct kinds of visual tracking in the recovered code:

- **Recognized-object tracking:** detect boxes, predict their movement with Kalman filters, match boxes between frames with a Hungarian assignment algorithm, and maintain a selected target.
- **Arbitrary-region tracking:** train and run the processor's hardware KCF tracker on a selected patch of the image. This can follow a region that is not one of the detector's known object classes, provided the region has enough usable visual detail.

The shipped configuration calls its tracker `bytetrack`. That name is **not enough to establish that this build implements the complete ByteTrack algorithm**. The executable's traced tracking route visibly uses Kalman prediction, box overlap, and Hungarian matching. It is reasonable to describe that recovered route as SORT-style. Its relationship to the configured ByteTrack options remains unresolved.

This is static firmware analysis. I extracted files and models, read configuration and startup scripts, and disassembled selected ARM64 functions. I did not execute the firmware, flash a device, connect to a module, or measure tracking performance. Findings below distinguish direct evidence, inference, and vendor release claims.

## 1. What was inside the update

Input image: `C:\Users\nickt\Downloads\SIYI_AI__II_v1_1_0_2026-02-07_1d57eb66\SIYI AI  II_v1.1.0 2026-02-07\SIYI_AIModule.bin`.

| Item | Finding |
| --- | --- |
| Original image size | 61,175,959 bytes |
| SHA-256 | `70ce086d0d2c9655eb2cdc3983dcba882a56c6d0a404e2a102acaf2c679b9729` |
| Container | 136-byte SIYI header followed by gzip-compressed tar |
| Header fields recovered | ASCII MD5 at offset 0; filename at offset 64; little-endian 64-bit payload size at offset 128 |
| Compressed payload | 61,175,823 bytes; MD5 `5b32e9f5d7faf3f519a38d499edd6e24` matches the header |
| Uncompressed tar | 75,898,880 bytes; 30 entries, including 19 regular files and 11 directories |
| Main program | `/app/siyi_ai_928`, 12,724,752 bytes; 64-bit little-endian AArch64 Linux ELF |
| Main program SHA-256 | `f0006784800e161c1993f960086a4442a99b2c41297073f2298d6c4ec5942c78` |
| Program build | February 2, 2026, 11:18:33 +0800; revision 2640 in build metadata |
| Platform evidence | Startup loads `load_ss928v100`; embedded SDK version names `SS928V100V2.0.2.2 B090`; compiler targets Cortex-A53 |
| Kernel update | `/app/image/kernel.bin`, 10,628,179 bytes; update script targets Linux 4.19.90, built January 8, 2026 |

The gzip integrity check, declared payload length, outer MD5, and model-archive MD5 all passed. All regular tar entries were extracted at their declared sizes. Six old model filenames are zero-length entries; the actual current models are in the separate archive. The zero-length entries do not show that the active models are missing.

The model archive is encrypted, but the main program itself contains the password used at its extraction call. I followed that call and unpacked the archive successfully. Its `models.7z.sign` file is a text MD5 checksum, not a public-key signature. This observation concerns this archive's integrity mechanism; it does not establish the device's complete firmware-authentication or secure-boot behavior.

The update is **not a complete dump of the module's storage**. It relies on existing system files, processor drivers, and shared libraries that are not all included here.

Evidence: [package summary](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/package-summary.json), [file manifest](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/manifest.json), [model manifest](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/model-manifest.json), [read-only extraction script](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/inspect_package.py).

## 2. The video and control pipeline

```text
SIYI camera
  | compressed video over the camera's private TCP protocol
  v
Module video queue -> hardware video decoder -> frame processing
                                             |
                              resize / letterbox / color conversion
                                             |
                          +------------------+------------------+
                          |                                     |
                    YOLOv6 detector                       hardware KCF
                          |                              selected image patch
                  box matching + prediction                     |
                          +------------------+------------------+
                                             |
                           selected target position + type + status
                                             |
                          private TCP command 0xAB -> camera
                                             |
                                    camera -> gimbal steering

Processed frames -> overlays -> video encoder -> RTSP viewing
Video output also has a separate HDMI display path.
```

The module connects to the camera configured in `param.ini`, normally `192.168.144.25:37256`. Its camera-client table accepts video command `0x90`; the handler removes a six-byte prefix and queues the remaining compressed data. Decoder and encoder functions support H.264/H.265-related paths. Thus the recovered normal camera-input route is a SIYI TCP video transport, rather than evidence of a general-purpose RTSP camera client.

The application divides processing between worker threads. The recovered thread functions include preprocessing, NPU execution, postprocessing/tracking, and plate recognition. Queues and synchronization connect these stages. This design permits decoding, analysis, and output work to overlap, although it does not prove any particular frame rate.

The neural-network input configuration is 640 by 640 with `LETTERBOX` resizing. Letterboxing preserves the picture's aspect ratio by padding the resized image. The postprocessing code calls `ip_decode_letterbox`, which maps detector boxes out of that padded coordinate space. This matters because simply treating a padded 640-square image as the original camera image would put boxes in the wrong place.

The program also converts NV21/YVU420SP frames to BGR for image operations, and converts processed frames back for video output. Its AI output function sends frames to the hardware encoder and releases the original video buffers.

Evidence: `ai_product_main_init` at `0x56c8b0`; [camera video handler](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/client_0_camera_sdk_video_stream_data_action.txt) at `0x56ed00`; [preprocessing](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/local_5bb750.txt) at `0x5bb750`; [NPU worker](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/local_5b9d90.txt) at `0x5b9d90`; [postprocessing and tracking](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/local_5ba2c0.txt) at `0x5ba2c0`.

## 3. What it can recognize

The recovered model archive contains four compiled models and two class-label files, plus a font for text overlays.

| Pipeline | Packaged model | Labels / purpose |
| --- | --- | --- |
| Visible-light detection | `yolov6m_9cls_npu_2.om`, 33,118,059 bytes | `person`, `car`, `bus`, `truck`, `airplane`, `boat`, `normal_ins`, `fire`, `smoke` |
| Thermal detection | `onnx_yolov6m_flir_640_yvu420sp_svp_npu.om`, 35,442,015 bytes | `person`, `car`, `bus`, `cyclist`, `bike`, `truck` |
| Plate detection | `plate_detect_no_cal_npu.om`, 3,951,413 bytes | Locates a license plate |
| Plate recognition | `plate_rec_color_no_cal_npu.om`, 604,045 bytes | Recognizes plate content and color |

`normal_ins` is the literal class label. Interpreting it as a normal electrical insulator is plausible given the separate `YOLO_INSULATOR` model slot, but the label alone does not define the precise training category.

The visible and thermal models have compiled `PICO` container headers; the plate models have `IMOD` headers and Ascend/Huawei-style graph metadata. These are executable-model formats for the device's neural accelerators. They are **not recovered original ONNX files, training code, or training datasets**. The YOLOv6 identity is supported by model names, graph strings, and actual YOLOv6 postprocessing functions in the application.

The configuration enables the visible model and thermal model. It disables the separate insulator slot and the empty user-model slot. Plate recognition is enabled and the supplied locale is `CN`/`zh`. The visible model already includes `normal_ins`; disabling the additional insulator slot does not remove that class from the visible model.

Configured detector defaults are a confidence threshold of **0.6** and a non-maximum-suppression threshold of **0.3**. Suppression removes overlapping duplicate detections. The traced postprocessing call allows up to 100 result entries; this is a code limit at that call, not a guarantee that 100 objects can be tracked effectively.

These labels establish what categories were packaged, not recognition accuracy. Fire/smoke labels do not prove a validated fire-alarm system; an airplane class does not establish reliable small-drone detection.

Evidence: [configuration](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/extracted/app/cfg/ai/configs/configs.json), [visible labels](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/models/configs/classes_9cls.name), [thermal labels](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/models/configs/classes_flir.name), `ap_npu_output_onnx_yolov6_result` at `0x574850`.

## 4. How tracking works

### Recognized objects

The detector supplies boxes, class IDs, and scores. The code converts those boxes to tracking records and calls `Detect_track::Run_Detect_track2`.

That function calls:

- `KalmanTracker::predict`, to estimate the next box position.
- `GetIOU`, to compare predicted and observed boxes by overlap.
- `HungarianAlgorithm::Solve`, to assign new detections to existing tracks.
- `KalmanTracker::update`, to correct a matched track with the new observation.
- Selection/overlap routines, target acknowledgments, and tracking-status output.

In plain language, it asks: **which object in this frame is probably the same object we were following in the previous frame?** A selected object then supplies the target center used for camera steering. Unmatched observations and tracks have creation/removal paths.

The selected-target code handles temporary loss and final loss separately. One observed missing-target counter compares against 180 before declaring loss and clearing tracking state. That is an iteration counter in a particular branch, **not a verified six-second timeout**. Its wall-clock duration depends on how often that branch runs.

The config contains ByteTrack-style thresholds (`0.25` high/new, `0.1` low, `0.8` match), `track_buffer=30`, and `fuse_score=true`. The parser reads these options. However, the verified tracking call chain above does not by itself establish ByteTrack's defining second association pass over low-score detections. Nor should the configured buffer of 30 replace the actual counter found in the selected-target branch.

For algorithm context, the original [SORT implementation](https://github.com/abewley/sort) describes Kalman/Hungarian tracking; the original [ByteTrack implementation](https://github.com/FoundationVision/ByteTrack) explains association with low-score detections. Those references explain the terminology; the SIYI identification rests on this firmware's instructions.

Further inspection recovered the recognized-object Kalman initializer `KalmanTracker::init_kf` at `0x5c1998`. It constructs a **7-state, 4-measurement, float32** OpenCV filter. The state is `[cx, cy, area, width/height, vx, vy, v_area]`; the measurement is `[cx, cy, area, width/height]`. The update routine converts a rectangle into center, area, and aspect ratio. Its transition matrix is:

```text
F = [1 0 0 0 1 0 0
     0 1 0 0 0 1 0
     0 0 1 0 0 0 1
     0 0 0 1 0 0 0
     0 0 0 0 1 0 0
     0 0 0 0 0 1 0
     0 0 0 0 0 0 1]
H = [I4 | zeros(4,3)]
Q = 0.01 * I7
R = 0.1  * I4
P_initial = I7
```

Here `I4` and `I7` mean identity matrices. `Q` is process-noise covariance, `R` measurement-noise covariance, and `P_initial` initial posterior uncertainty. These names are mapped using [OpenCV's declared member order](https://github.com/opencv/opencv/blob/4.x/modules/video/include/opencv2/video/tracking.hpp): the initializer sets tracker offsets `0x1f8`, `0x258`, and `0x378` respectively. The `0.01` and `0.1` double constants are at `0x5c2ee8` and `0x5c2ef0`. The transition uses one prediction step, without a seconds-based `dt` in this initializer. Its velocities are therefore changes per prediction step. These are static initializer values, not a live read or tuning recommendations for another camera pipeline.

Evidence: [Kalman initialization](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/_ZN13KalmanTracker7init_kfEN2cv5Rect_IfEE.txt), [measurement update](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/_ZN13KalmanTracker6updateEN2cv5Rect_IfEE.txt), [prediction](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/_ZN13KalmanTracker7predictEv.txt), [decoded constants](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/tracking-constants.json), [extraction script](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/extract_tracking_constants.py).

### Arbitrary image regions

The KCF route takes a selected region, checks its bounds and visual complexity, generates tracker-training data, and uses processor IVE functions including:

```text
ss_mpi_ive_kcf_create_obj_list
ss_mpi_ive_kcf_create_gauss_peak
ss_mpi_ive_kcf_create_cos_win
ss_mpi_ive_kcf_get_train_obj
ss_mpi_ive_kcf_proc
ss_mpi_ive_kcf_get_obj_bbox
```

This route follows the appearance of a patch rather than requiring a known detector class. The visual-complexity check can reject unsuitable regions before training. That helps explain why clicking a featureless area may fail even when the object is visible to a person.

The KCF output is also compared with detector boxes, and the code can attach a recognized class to the tracked region when overlap supports it. The two paths therefore interact; they are not entirely separate applications.

The KCF initializer at `0x576020` also contains these tuning defaults:

| KCF parameter | Stored integer | Decoded value / meaning |
| --- | --- | --- |
| Template interpolation / adaptation | 393, Q1.15 | `393 / 32768 = 0.0119934`, approximately 1.2% blend |
| Regularization (`lamda`) | 6, Q0.16 | `6 / 65536 = 0.0000915527` |
| Feature-normalization truncation | 819, Q4.12 | `819 / 4096 = 0.199951` |
| Gaussian-kernel bandwidth (`sigma`) | 102, Q0.8 | `102 / 256 = 0.398438` |
| Processing response threshold | 32 | Hardware response units; not a probability |
| ROI padding | 48, Q3.5 | `48 / 32 = 1.5` |
| Object-list capacity / output-box capacity | 1 / 1 | One selected KCF target |
| Output-box response threshold | 0 | Separate from processing threshold 32 |

The four packed fields and response threshold are written together as `0x2066033300060189` at context offset `0x600`: little-endian bytes decode to `393, 6, 819, 102, 32`. The field names and fractional formats match the vendor-authored [Shenshu IVE header](https://github.com/openhisilicon/HIVIEW/blob/master/mod/mpp/3403/inc/hisisdk/ot_common_ive.h), preserved in a public SDK mirror. The SIYI values come from the firmware instructions, not the mirror's sample defaults. ROI conversion shifts x/y by eight fractional bits and rounds width/height down to even pixels. These hardware parameter values cannot be assumed equivalent to settings in another KCF implementation. The complete visual-complexity rejection threshold and every runtime override remain unresolved.

For a new A8 tracking filter, useful measurements beyond these defaults are actual frame timestamps and gaps, capture-to-processing/command latency, stationary-target localization variance, apparent image motion caused by gimbal movement, zoom-dependent pixel-to-angle calibration, and behavior during occlusion/outliers. An optional simpler center-only design is `[cx, cy, vx, vy]` with measured `dt` in seconds. Its noise matrices must be chosen in its own units; the SIYI seven-state constants are a reference, not a calibration of that design.

The selection handler reads a flag followed by four little-endian 16-bit coordinates. Downstream code accepts a point-style selection when the second coordinate pair is zero, and checks corner ordering for a rectangle. The full meaning of every selection-flag value and return code has not been mapped.

Evidence: [detector tracking](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/_ZN12Detect_track17Run_Detect_track2EP14tracking_box_tiP10ot_svp_imgii.txt) at `0x595120`; [selection logic](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/apt_select_ai_target.txt) at `0x577930`; [KCF setup](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/kp_kcf_init.txt) at `0x576020`; KCF processing and loss handling inside `0x5ba2c0`.

## 5. How it tells the camera where to point

This is the clearest link to the earlier A8 Mini analysis.

`apt_tell_ai_switch_to_extern` sends **command `0xA3` with one byte** indicating whether AI processing is enabled. `apt_send_info_of_ai_detect` sends **command `0xAB` with ten bytes**. Both use the camera-client transport. The module-side sender explicitly constructs the same long framing recovered in the A8 camera:

```text
55 66 AA BB | flags:u8 | payload_length:u32 LE | sequence:u16 LE
           | command:u8 | header CRC32 | payload | packet CRC32
```

The header CRC covers the first 12 bytes. The sender then appends the payload and calculates the packet CRC over header-plus-payload. The module's CRC table and instructions use the MSB-first polynomial `0x04C11DB7`, seed zero, and no final XOR: `123456789` produces `0x89a1897f`. This matches the earlier camera result and is not ordinary reflected `zlib.crc32`. These packets are separate from the short external SDK's format. Command IDs only make sense together with their transport and device direction.

The earlier [A8 Mini firmware study](C:/Users/nickt/siyi_sdk/docs/firmware-ai-tracking-analysis.md) traced the receiving `0xAB` handler forwarding target data to the gimbal, and the gimbal's pixel-error controller. This module image supplies direct evidence of the sending half. The prior camera study concerns camera v0.3.7/gimbal v0.4.9, released later than this module package; matching code establishes structural compatibility, not a tested version combination.

### Other commands the module sends to the camera

Direct outgoing call sites in this build establish the following commands on the long camera protocol:

| Command | What the module asks the camera to do | Payload traced |
| --- | --- | --- |
| `0xA3` | Enable or disable AI tracking mode | One byte, `1` or `0` |
| `0xAB` | Accept the current visual target result for steering | Ten-byte center/dimensions/class/status record |
| `0x97` | Focus at an image position | Five bytes: mode `1`, then X and Y as little-endian 16-bit values |
| `0x83` | Report video encoding parameters | One byte, `1`; response is parsed for codec and resolution |
| `0x90` | Enable the camera video stream to the module | One byte, `1`, sent after processing encoding parameters |
| `0x80` | Report recording status | One byte, `1` |
| `0xA5` | Set thermal pseudocolor/palette state | One byte, `0`, in thermal-related AI setup/switch branches; a controller-request route also forwards a palette value |

The last five are supporting camera operations, not direct pan/tilt motor commands. Target-position autofocus is called from recognized-object tracking code. Video setup is handled by `client_0_camera_sdk_get_encode_param_action` at `0x56ed40`; request helpers are at `0x56ef50` and `0x56eef0`. The literal outgoing calls are collected in [outgoing command evidence](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/outgoing-command-calls.txt).

The module also has a generic controller-to-camera forwarding route, so forwarded requests can carry additional command IDs. This table covers the internally generated commands and the palette route traced here, not every possible relayed controller request.

### Ten-byte target data and an important mode distinction

The local camera protocol describes the ten-byte coordinate stream as:

| Offset | Type | Documented meaning |
| --- | --- | --- |
| 0 | `uint16` | Target center X |
| 2 | `uint16` | Target center Y |
| 4 | `uint16` | Target box width |
| 6 | `uint16` | Target box height |
| 8 | `uint8` | Target class; `255` means arbitrary object |
| 9 | `uint8` | Tracking status |

The documented coordinate reference is **1280 by 720**. Status values are `0` recognized-object tracking, `1` temporary loss, `2` lost, `3` canceled, and `4` arbitrary-object tracking.

**This module's KCF branch writes 1280 and 720 into the third and fourth words, rather than the tracked patch's actual dimensions.** At `0x5bb254` it constructs `0x02d00500` and stores it at those two fields. At `0x5bb298` it constructs `0x04ff`, storing class `255` and status `4`. The center is scaled into the 1280-by-720 reference space. Other detector-based paths scale box dimensions, and some branches also replace them with the full-frame constants.

That is a refinement of the earlier general box-field interpretation. An emulator should preserve the mode-specific values actually sent by the module. Treating all ten-byte packets as identical box geometry could alter behavior, especially where the camera uses dimensions for zoom-related decisions. The reason for using full-frame values in these branches is an inference; their presence is directly visible in the instructions.

For example, a centered arbitrary-region target would have payload `80 02 68 01 00 05 D0 02 FF 04`: center `(640, 360)`, dimension fields `(1280, 720)`, arbitrary-object class `255`, and status `4`. This is an illustrative encoding derived from the instructions, not a captured device packet.

Turning AI processing off has a concrete cleanup path: clear tracking fields, set status `3`, send a target update, restore the video-processing-to-encoder binding, and notify the camera with `0xA3` disabled.

There is also a handler named `cancel_ai_target_tracking` for `0xAC`, but the inspected wrapper/handler only reads current tracking state and sends an acknowledgment. I did **not** find it directly clearing state. Consequently, its name alone should not be treated as proof of a working cancel operation. The verified AI-off cleanup route is stronger evidence.

Evidence: [AI enable sender](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/apt_tell_ai_switch_to_extern.txt) at `0x5774d0`; [target sender](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/apt_send_info_of_ai_detect.txt) at `0x5773a0`; [long-protocol encoder](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/long_protocol_tcp_client_send.txt) at `0x55f6d0`; KCF output at `0x5bb224`-`0x5bb2f0`; [AI switch lifecycle](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/apt_set_ai_switch.txt); [cancel handler](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/server_0_camera_sdk_cancel_ai_target_tracking_action.txt).

### Timing and command delays

The traced target path does **not establish explicit command-delay compensation**. The ten-byte target payload carries coordinates, dimensions, class, and status; it has no capture timestamp, velocity, or intended execution time. The long-protocol header adds a sequence number but no timestamp. The camera handler forwards the payload to the gimbal without computing its age. Kalman prediction supports visual track association; the inspected initializer uses a one-step transition, and no measured network/actuation delay is supplied to that filter in the traced route.

The module's link-write callback enqueues encoded bytes using `queue_puts(1, ...)`. The queue implementation is a FIFO ring buffer. The sender worker at `0x55abd0` drains up to **256 bytes** per pass, calls `send`, then requests a **1,000-microsecond sleep** before its next pass. This is a requested polling interval, not an end-to-end latency guarantee. The inspected queue/sender path has no target-aware replacement of old queued coordinates with newer ones. Queueing and TCP can therefore accumulate old updates when transmission cannot keep up; actual delay was not measured.

Once a target reaches the gimbal, routine `0x080337f4` overwrites a single target record and sets a new-data flag; it does not create a timed movement trajectory for each received target. Its visual controller operates locally. Missing-data counters and controller-reset branches are present, but their exact time units and complete expiry behavior remain unresolved. This architecture keeps physical feedback local and applies newly received observations, but does not prove compensation for the age of those observations. Successful target forwarding also has no private motor-completion acknowledgment.

Evidence: [link-write queueing](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/ai_tcp_client_ai_link_write.txt), [FIFO writer](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/write_block_queue.txt), [FIFO reader](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/read_block_queue.txt), [TCP sender](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/local_55abd0.txt), [camera target forwarding](C:/Users/nickt/siyi_sdk/.tools/firmware-analysis/disassembly/camera_sdk_set_ai_track_target_info_action.txt), [gimbal target storage](C:/Users/nickt/siyi_sdk/.tools/firmware-analysis/disassembly/gimbal-range-80337c0.txt).

## 6. Interfaces and other functions

| Interface | Recovered setting / behavior |
| --- | --- |
| Module IP | Application config `192.168.144.60`, mask `255.255.255.0`, gateway `192.168.144.1` |
| Camera connection | Configured outbound TCP connection to `192.168.144.25:37256` |
| Module long-protocol server | TCP `37256`; separate from the outbound camera connection |
| Module short SDK | TCP and UDP `37260` |
| RTSP output | Starts at port `554`, stream name `video0`; tries higher ports if binding fails |
| UART SDK | `/dev/ttyAMA4`, configured at 115200 baud, 8 data bits, one stop bit, no parity |
| HDMI | Dedicated display setup, scaling and centering logic |

The boot network script initially uses a different gateway and has a branch selecting `.75`; the main application subsequently reads and applies `param.ini`. Those are different startup layers. The shipped config is the strongest indication of intended application settings, but no live address was checked.

The module's long-protocol dispatch table exposes:

| Command | Traced purpose |
| --- | --- |
| `0x74` / `0x75` | Read / set network configuration |
| `0xA2` / `0xA3` | Read / set AI processing switch |
| `0xAA` | Select tracking target |
| `0xAC` | Named cancel handler, with the limitation described above |
| `0xA9` | Get / set model ID |
| `0xAD` | Get / set AI confidence threshold |
| `0xAE` | Load / unload model |
| `0xAF` | Plate recognition mode |
| `0xD5` | Object statistics / class filtering |
| `0xF1` | Model package management |

The short SDK has separate IDs: `0x03`/`0x04` for AI-switch get/set, `0x05` for tracking-state query, `0x06` for target selection, and `0x08`/`0x09` for target-stream state. These must not be interchanged with the camera's public SDK IDs or the long module protocol.

**Object counting** counts the current detector results by class and publishes a packet containing mode, model ID, class count, and one-byte per-class counts. Its buffer is freshly allocated for each call. The traced function does not establish persistent unique-person counts, tripwire crossing, or totals over time. Class-filter settings provide per-class masking and class-name reporting.

**License-plate processing** has a distinct worker: detect a plate, crop and transform it, run the recognition model, decode content and color, and maintain plate-result maps. Configuration and locale checks constrain available plate modes. The shipped `CN` configuration does not establish international plate coverage.

**Model management** includes negotiated transfers, task IDs, ordered chunks, MD5 checks, timeouts, loading from storage, and user-model paths such as `/mnt/user_model.siyimodel`. A disabled `YOLO_USER` slot and these routes show extension support; they do not prove that an arbitrary ONNX model can simply be copied onto the device and used.

**Logging** writes raw AI logs, has encryption/export routines, and can copy exported logs to mounted storage. This is local diagnostic behavior in the inspected code; it is not evidence of remote cloud reporting.

Evidence: [dispatch tables](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/command-tables.txt), [network parameters](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/extracted/app/param.ini), [RTSP setup](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/SS_RtspServerStart.txt), [statistics function](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/apt_push_datect_obj_statistics.txt), [plate worker](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/local_5b92c0.txt), [model manager strings](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/strings.txt).

## 7. Startup, upgrade behavior, and this release's changes

The startup script loads processor drivers, configures Ethernet, starts a Telnet daemon, performs pinmux setup, copies the application and updater into `/run`, starts the updater, checks the kernel update, and starts the AI application. Missing application/updater files have `/opt` fallback paths. It also clears `/mnt` at boot; this is a statement about the supplied script, not an action performed during this analysis.

The kernel script compares `uname -a` against its expected kernel-version string. If they differ, it erases `/dev/mtd1`, writes `kernel.bin`, syncs, and reboots. If the versions match, it removes the kernel-update directory. The separate updater contains package-version checks, MD5 verification, archive extraction, and reboot paths; its full compatibility policy was not reconstructed.

The included Chinese release notes identify **v1.1.0, February 7, 2026** and claim:

- Added object statistics, recognition filtering, network setup through UniGCS, and logging.
- Improved tracking logic, HDMI picture centering, and tracking frame rate.
- Fixed plate-recognition accuracy regression, H.264-related RTSP output problems, selection-box misalignment during rapid stream switching, and selection failure after changing streams or resolution during tracking.
- Removed private-stream output.

The binary supports the last item directly: its server-side private video handler logs that SIYI FPV private streaming is no longer supported. **Private camera video input still exists.** The removal does not mean the module stopped receiving camera video or stopped providing RTSP output.

Code for counting, filtering, network configuration, and logging is present. The improvement and bug-fix claims cannot be independently measured from this one image; that would require another firmware version or hardware testing. The AI-enable path also rejects stored stream dimensions above 1920 by 1080, so neural input size, accepted video size, HDMI size, and outgoing tracking coordinates should be treated separately.

Evidence: [startup](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/extracted/etc/init.d/S90autorun), [kernel update script](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/extracted/app/image/image.sh), [release-note text](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/release-notes.txt), [removed private-stream handler](C:/Users/nickt/siyi_sdk/.tools/ai-module-analysis/disassembly/server_0_camera_sdk_video_stream_data_action.txt).

## 8. What this establishes, and what remains unknown

**Established from this package:** the application platform, model files and labels, configured thresholds and feature flags, decoder/encoder pipeline, actual Kalman/Hungarian and KCF calls, module-to-camera command IDs and framing, mode-dependent target payloads, statistics construction, and startup/update scripts.

**Strong interpretation:** the module performs visual target localization while the camera/gimbal perform motor control; recognized-object tracking follows a SORT-style association route; arbitrary-region tracking uses hardware KCF. The camera-side motor-control conclusion also uses the separate A8 firmware study, not motor code recovered from this module image.

For the inspected **A8 Mini gimbal 0.4.9 svn10601**, further decoding of its startup data recovered compiled visual-tracking controller defaults: horizontal and vertical **P=3, I=0.05 per update, D=0**, with output clamps **±1600** and integral clamps **±500 horizontal / ±1500 vertical**. Its size/zoom controller starts with **P=5, I=0, D=0**, output clamp **±500**. These are gimbal-side values, not AI-module detector thresholds or a complete specification of the inner motor/stabilization loops. See the [A8 controller analysis](C:/Users/nickt/siyi_sdk/docs/firmware-ai-tracking-analysis.md) and [decoded initial values](C:/Users/nickt/siyi_sdk/.tools/firmware-analysis/disassembly/a8-controller-initial-values.txt). Exact update timing and live runtime overrides remain unverified.

**Not established:** measured FPS or latency, maximum useful tracking distance, detection accuracy, identity preservation under occlusion, exact live timeout durations, complete flag/status semantics, every model-manager command format, universal camera-version compatibility, and physical stop/zoom behavior. No appearance-embedding model was identified in the recovered model archive; that is not proof that no other identity mechanism exists anywhere in the system.

For our SDK, the useful result is that the previously recovered A8 coordinate-input route is now corroborated by the actual module's sender. Any future replacement should imitate its enable/disable lifecycle, coordinate scaling, selected tracking mode, class/status bytes, and mode-dependent dimensions. The firmware provides concrete packet and algorithm evidence; testing on a stationary camera is still needed to establish physical behavior.

## Inspection record

The analysis directory is `C:\Users\nickt\siyi_sdk\.tools\ai-module-analysis`. It contains the extracted update, recovered model archive contents, hashes, raw strings, symbol index, dispatch tables, and disassembly evidence. These files are ignored by Git through the existing `.tools/` rule.

The main executable retains 14,741 named dynamic symbols, including 14,065 with nonzero addresses. I initially disassembled 156 selected named functions, then expanded into unnamed processing workers using exception-unwind function boundaries and direct call references. The final saved analyzer emits 216 named and unnamed functions. This was selective tracing of relevant behavior, not exhaustive decompilation of every function. Original C/C++ source was not recovered.

Reproduce package validation and extraction with the saved `inspect_package.py`, using the bundled Python runtime and the original `.bin` path. `analyze.py` disassembles selected exported functions with pyelftools and Capstone. Saved `local_*.txt` files additionally cover unnamed functions traced during this inspection. Comments resolving register-derived strings in disassembly are convenience annotations; conclusions should be checked against the actual instructions and constants.

No SDK implementation or existing firmware report was changed. This report records the new module-side evidence and its implications separately.
