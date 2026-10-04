# Alternatives to joystick control for the SIYI A8 Mini

Research date: October 1, 2026. Scope: this SDK, the checked-in SIYI protocol, current vendor manuals, and documented ArduPilot/QGroundControl controls. No camera movement, tracking performance, or flight behavior was tested during this research.

The A8 Mini already has the command needed for a better interface: **move to a target yaw and pitch**. My recommendation is to combine **click an object to center it**, **drag to look around**, and **saved views**. Add **select an object to track** as a separate feature when tracking software or SIYI's optional tracking module is available.

Those gestures are proposals for this dashboard. The underlying angle commands are already implemented in the SDK.

| Method | What the operator does | What makes it work | Assessment |
| --- | --- | --- | --- |
| Click to center | Click a visible object; the camera brings it to the center | Video-to-angle calculation and existing target-angle command | Best main interaction for inspection |
| Drag to look | Hold a mouse button and move the mouse; release to leave the selected view | Mouse movement changes an angle target | Easiest useful improvement |
| Absolute pointing panel | Click a yaw/pitch diagram or move a pointer while holding a button | Pointer position maps directly to target angles | Predictable even with delayed video |
| Box to inspect | Draw a box; center it and zoom to fit | Click-to-center plus resolution-aware digital zoom | Useful extension for inspecting details |
| Select to track | Click an object or draw a box once; keep it framed automatically | SIYI tracking module, or tracking software controlling the gimbal | Largest reduction in operator workload; more integration work |
| Map target | Select a geographic point; keep looking at it as the aircraft moves | Aircraft position, attitude, target elevation, and a pointing controller | Best for fixed ground locations |
| Saved views and scan paths | Click a named view, or select a scan region | Stored angle targets and a sequence controller | Useful immediately; avoids repeated manual positioning |
| Head or device orientation | Turn your head or a handheld device to look around | An orientation sensor mapped to target angles | Feasible experiment; needs extra input hardware and calibration |

**What the firmware and this repository already provide**

The checked-in [vendor protocol](C:/Users/nickt/siyi_sdk/SIYI_SDK_PROTOCOL.pdf) specifies `0x0E` for a yaw/pitch target, `0x41` for a single-axis target, and `0x07` for rotation speed. Angle fields use 0.1-degree increments. That is command resolution, not a guarantee of pointing accuracy. The copy currently linked from SIYI's download page is byte-for-byte identical to the checked-in PDF. [Vendor protocol download](https://res.siyi.biz/oss/other/2026/06/23/SIYI_Gimbal_Camera_External_SDK_Protocol_Update_Log_V0_1_1_fc8b51eb.pdf).

| Capability | Existing SDK entry point | Why it matters |
| --- | --- | --- |
| Move both axes to a target | `set_attitude(yaw_deg, pitch_deg)` | Supports clicks, drag position control, presets, and scan paths |
| Update a target without waiting for a reply | `set_attitude_nowait(...)` | Supports continuously changing mouse or tracking targets |
| Move one axis | `set_single_axis(axis="yaw" or "pitch", angle_deg=...)` | Supports a yaw dial or pitch slider independently |
| Read actual orientation | `get_gimbal_attitude()` | Checks where the camera actually is |
| Receive orientation continuously | `request_gimbal_stream(...)` and `on_attitude(...)` | Supports feedback and completion checks |
| Change stabilization behavior | `capture(CaptureFuncType.LOCK_MODE/FOLLOW_MODE/FPV_MODE)` | Determines how the view responds to aircraft rotation |
| Set digital zoom | `absolute_zoom(...)`, `get_zoom_range()`, `get_current_zoom()` | Supports wheel zoom and box-to-inspect |

See [client implementation](C:/Users/nickt/siyi_sdk/siyi_sdk/client.py:695) and [command explorer catalog](C:/Users/nickt/siyi_sdk/web_ui/command_explorer.py:51). The explorer already exposes angle commands, so the main missing piece is the operator interface and its coordinate conversion. The dashboard's normal control path currently calls a speed-control endpoint. [Dashboard server](C:/Users/nickt/siyi_sdk/web_ui/server.py:524).

The SDK explicitly describes a `0x0E` reply as orientation at receipt, **not confirmation that the requested position has been reached**. A UI should show the requested position separately from the reported position and confirm arrival using subsequent telemetry. The SDK also avoids retrying old angle targets. [Position command behavior](C:/Users/nickt/siyi_sdk/siyi_sdk/client.py:762), [retry behavior](C:/Users/nickt/siyi_sdk/CHANGELOG.md:46).

**1. Click a visible object to bring it to the center**

For example, click a roof near the right edge of the video. The application calculates the direction from the camera to that pixel and sends a new angle target. The operator chooses a subject instead of estimating how long to hold a direction control.

This can work with the bare A8 Mini. I found no documented bare-camera command that means "aim at this video pixel." The application supplies that calculation using the video geometry and camera orientation. Pixel coordinates in the vendor-wide autofocus command are for focusing optical zoom cameras; they are not an A8 Mini pointing API.

This interaction already exists through another integration route: ArduPilot documents QGroundControl's on-screen **Click to point** control, including entering horizontal and vertical field of view. [ArduPilot controls](https://ardupilot.org/copter/docs/common-mount-targeting.html).

For an initial stationary, level-camera prototype:

1. Convert the browser click to a pixel in the displayed image, accounting for black borders, resizing, cropping, and any rotation or mirroring.
2. Convert that pixel to a viewing ray using the camera's lens calibration and current zoom.
3. Express that direction in the coordinate convention expected by the gimbal and send a reachable angle target.
4. Observe subsequent orientation reports and the video to check arrival.

An approximate horizontal calculation is:

```text
fx = image_width / (2 * tan(horizontal_FOV / 2))
horizontal_offset = atan((clicked_x - image_center_x) / fx)
```

For a hypothetical 1920-pixel image with an 81-degree horizontal field of view, a click at x=1440 is about **23.1 degrees** to the right of center. A simple linear pixel-to-degree mapping gives 20.25 degrees and introduces avoidable error. The 81-degree figure is published for the lens; verify the effective field of view of the actual stream and crop. [A8 Mini manual v1.10, printed pages 15-16](https://res.siyi.biz/oss/other/2026/06/15/A8_mini_User_Manual_v1_10_563cde30.pdf), [OpenCV camera geometry](https://docs.opencv.org/4.13.0/d9/d0c/group__calib3d.html).

At large pitch angles or with camera roll, use the full viewing ray and orientation transform. Simply adding independent horizontal and vertical offsets to yaw and pitch is an approximation. Calibrate the angle reference, axis signs, and mounting direction before flight use.

A click chooses a viewing direction at that moment. It does not by itself keep a moving object centered or keep a geographic location centered as the aircraft changes position. Those require tracking or a geographic target.

**2. Drag the view to look around**

Hold a mouse button over the video and move the mouse. Mouse displacement changes the requested angle. When the mouse stops, the requested angle stops changing; releasing leaves the final selected target in place.

Capture the initial angle target and pointer position when the drag starts:

```text
requested_yaw   = initial_yaw   + horizontal_mouse_displacement * sensitivity
requested_pitch = initial_pitch - vertical_mouse_displacement   * sensitivity
```

Axis signs are examples and need checking for the installed camera. Offer a fine-adjustment modifier and reduce sensitivity when zoomed in. Mouse pointer capture lets a drag continue outside the video element; optional pointer lock can support longer movements.

Use mouse displacement from the drag origin, not the cursor's distance from the middle of the screen. A stationary cursor then corresponds to a stationary angle target. This gives a different feel from a spring-centered speed joystick and requires no lens calibration for basic manual aiming.

Send the most recent target at a bounded rate and discard superseded updates. Start conservatively, for example at 10-20 updates per second, and tune from measured camera response. The SDK's support for rapid sending does not establish the camera's effective motion bandwidth. For smooth motion, limit target changes per second in the application; the documented angle command has no requested-duration field.

**3. Use a pointing panel for direct mouse-pointer control**

Display a small two-dimensional panel: horizontal position represents yaw and vertical position represents pitch. Clicking it chooses an angle. Show both requested and actual positions.

This is the cleanest interpretation of "the camera follows the mouse pointer": the pointer chooses a fixed direction in a fixed coordinate panel. Moving it to the same point gives the same target. Prefer click or hold-to-control so ordinary mouse navigation does not move the camera.

For this SDK, use the documented command envelope of **yaw -135 to +135 degrees and pitch -90 to +25 degrees**. Those values appear in the protocol, the current A8 manual, the SDK constants, and ArduPilot's SIYI configuration. SIYI's public specification page advertises a wider range, so use the documented SDK envelope until the installed hardware/firmware proves otherwise. [SDK limits](C:/Users/nickt/siyi_sdk/siyi_sdk/constants.py:398), [ArduPilot SIYI setup](https://ardupilot.org/copter/docs/common-siyi-zr10-gimbal.html), [SIYI product specification](https://www.siyi.biz/en/product/tri-axis-single-camera-gimbal/a8-mini/spec/).

Treat these as device targets. A compass panel labeled with world headings needs aircraft heading and the appropriate coordinate conversion; an SDK yaw number alone is not a verified north-referenced bearing.

Continuous pointer following over the moving video needs a different definition. Keeping the camera rotating while the pointer remains off-center is still speed-style control. Trying to maintain a scene feature under the pointer requires visual tracking. I would use a fixed pointing panel for continuous pointer control and a single click for video aiming.

**4. Draw a box to inspect a detail**

Draw a rectangle around a window, vehicle, or other detail. Aim at its center, confirm the movement, then select a digital zoom level that frames it. This combines two useful operations in one gesture and can also support a mouse-wheel zoom centered on the selected subject.

Zoom changes the effective field of view and must update the click calculation. Use the reported zoom range rather than assuming 6x is always available: this SDK documents and enforces resolution-dependent limits, including no digital zoom in 4K recording mode. Digital zoom magnifies the existing image; it does not provide optical detail. [Zoom implementation](C:/Users/nickt/siyi_sdk/siyi_sdk/client.py:646), [resolution limits](C:/Users/nickt/siyi_sdk/CHANGELOG.md:16).

**5. Select an object once and track it**

There are two practical routes.

**Use SIYI's optional AI Tracking Module II.** SIYI lists A8 Mini compatibility. Its current v1.2 manual documents a separate SDK at default address `192.168.144.60:37260`; command `0x06` accepts a target point or rectangle and a start/cancel action. This is an actual firmware API for selecting a visual target. [Module compatibility](https://www.siyi.biz/en/product/accessories/ai-tracking-module-v2/spec/), [module manual v1.2, pages 39-44](https://res.siyi.biz/oss/other/2026/06/15/SIYI_AI_Tracking_Module_II_User_Manual_v1_2_8dd26b16.pdf).

The manual has material inconsistencies: its target table describes a nine-byte payload with four coordinates, while its point-selection example uses five bytes; a tracking-output table also repeats an inconsistent command label. Verify the installed module version and packet behavior before implementing point/rectangle selection. This is documented capability, not a tested integration here.

The current A8 SDK is not a ready-made module client. The module uses a different command catalog: for example, its `0x06` selects a tracking target, while the camera protocol uses `0x06` for manual focus. Add a dedicated module adapter rather than pointing this camera client at the module and assuming the APIs match.

**Run tracking software on the companion computer or ground computer.** Initialize a tracker with the selected box, update its location in each frame, and command the gimbal to reduce its distance from the desired image position. OpenCV's CSRT tracker is one concrete candidate with box initialization and per-frame updates. Performance and target retention need measurement on the intended computer and video. [OpenCV tracker API](https://docs.opencv.org/4.13.0/d2/da2/classcv_1_1TrackerCSRT.html).

The SDK already provides decoded-frame callbacks and angle/speed commands. A speed command can be appropriate inside an automatic tracking loop: the operator still selects an object, and software handles the ongoing motion. Running the loop onboard is a promising way to avoid the radio round trip, but actual latency and compute capacity remain unmeasured.

Tracking can also keep the object at a chosen composition point, such as the left third of the image, rather than always centering it. On target loss, stop issuing corrections and show that tracking is lost. Keep manual drag available as an explicit takeover action.

**6. Choose a map location and keep looking at it**

For a building or fixed ground feature, a geographic target is often more useful than image tracking. Select its position and elevation; the pointing controller updates the camera direction as the aircraft moves.

ArduPilot already supports this via `MAV_CMD_DO_SET_ROI_LOCATION` and cancellation via `MAV_CMD_DO_SET_ROI_NONE`. It also has a SIYI driver. This is an alternative integration through the autopilot, subject to its setup and firmware, not an additional feature already implemented in this repository. [ArduPilot geographic-target commands](https://ardupilot.org/dev/docs/mavlink-gimbal-mount.html), [SIYI integration](https://ardupilot.org/copter/docs/common-siyi-zr10-gimbal.html).

A companion computer could instead calculate the direction using aircraft position, aircraft attitude, camera mounting alignment, and target coordinates, then issue SDK targets. The bare camera's `send_raw_gps()` is an aircraft-data input, not a "look at this target GPS" command.

For a ground object selected in the video, a pixel gives a ray, not its distance. Converting that ray into a geographic point needs terrain, a known surface/elevation, or another distance source. The A8 Mini has no laser rangefinder. Avoid presenting an unmeasured pixel-to-map estimate as an exact target location.

**7. Save views, define scan paths, or build a panorama selector**

Store named yaw/pitch/zoom views such as "Forward", "Straight down", or "Inspection view", then return with one click. The repository already has an angle-based [scan example](C:/Users/nickt/siyi_sdk/examples/gimbal_scan.py:15).

For a scan, choose bounds and image overlap, move through targets, wait for reported settling, and capture frames. The existing example uses timed pauses; a production scan should confirm position rather than assume a pause means arrival. Saved device angles will not keep looking at the same ground point after the aircraft moves.

A more creative extension is a previously captured panoramic strip. Click a region in the strip to select the corresponding direction, then show the live view there. This gives context outside the current narrow view. Mark the panorama's age: it contains older images, and aircraft movement changes their relationship to the current scene.

**8. Use head movement or a handheld device as the input**

An orientation sensor can feed the same angle-target interface. Add a "use this pose as center" action, adjustable sensitivity, filtering, limits, and hold-to-enable control. Mouse wheel or a separate input could control zoom.

This is an application/input-device experiment, not a special SIYI firmware capability. I would prioritize mouse controls first because they use the existing hardware and are easier to assess with this dashboard.

**Recommended build order and checks**

1. Add drag position control, a pointing panel, and named presets using the existing angle APIs. Confirm axis signs, target reference, limits, and behavior in each motion mode on a stationary camera.
2. Add click-to-center at 1x on a stationary camera. Calibrate actual stream geometry and test clicks across the image, including corners and black borders.
3. Add zoom-aware clicking and box-to-inspect. Repeat checks for every supported resolution/zoom combination.
4. Select the tracking route based on available hardware and onboard compute. Test target loss, cancellation, and manual takeover.
5. Add geographic targeting when aircraft telemetry, mounting calibration, and target elevation are available. Test it separately from holding a viewing direction.

For all changing targets, keep one camera command owner, use the latest target rather than a backlog, and show requested versus reported state. The current SDK documents UDP replies going to the most recent sender; separate experimental clients can interfere with feedback. Preserve stop handling for speed-based tracking and test cancellation of an active angle move. [Link behavior](C:/Users/nickt/siyi_sdk/CHANGELOG.md:52), [existing dashboard motion watchdog](C:/Users/nickt/siyi_sdk/web_ui/server.py:333).

Measure visible response and settling, not just command reply time. This SDK's frame timestamp is at decode, and the dashboard's frame-age metric measures time since receipt of a decoded frame. Neither establishes camera exposure time or full display delay. Initial click tests should therefore start from a settled view; accurate clicking during motion needs a measured relationship between the displayed frame and camera pose. [Frame model](C:/Users/nickt/siyi_sdk/siyi_sdk/stream/models.py:101), [dashboard timing explanation](C:/Users/nickt/siyi_sdk/web_ui/index.html:279).

LOCK mode holds a viewing direction during aircraft rotation; keeping a ground object framed during aircraft translation requires geographic targeting or visual tracking. The user-facing controls should make that distinction clear. [A8 Mini motion-mode descriptions, printed page 12](https://res.siyi.biz/oss/other/2026/06/15/A8_mini_User_Manual_v1_10_563cde30.pdf).

This investigation establishes available commands and plausible control designs. It does not establish the installed camera/module firmware, pointing accuracy, tracking speed, or end-to-end video delay.
