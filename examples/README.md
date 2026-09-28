# A8 Mini examples

Install the package with `pip install -e .`. The examples use the default camera address `192.168.144.25:37260`; adjust it for your network.

## Control and status

- `udp_control.py`: basic UDP connection, firmware, and attitude check. UDP does not require heartbeat frames.
- `system_info.py`, `soft_reboot.py`: device information and maintenance.
- `gimbal_rotation.py`, `set_attitude.py`, `single_axis_control.py`, `gimbal_modes.py`, `gimbal_scan.py`: gimbal positioning and modes.
- `subscribe_attitude_stream.py`, `attitude_control_loop.py`: attitude feedback and closed-loop control.
- `zoom_control.py`: manual and absolute digital zoom.
- `camera_capture.py`: photos, recording, and SD card media listing.
- `encoding_params.py`: recording/main/SUB encoding parameters. The SUB setting does not imply a separate RTSP URL.

## Video

Install one or more of `stream-opencv`, `stream-gst`, and `stream-aiortsp`. Automatic backend selection tries GStreamer, aiortsp, then OpenCV.

- `rtsp_opencv.py`: view the A8 Mini main RTSP stream with OpenCV.
- `rtsp_gstreamer.py`: view the same stream with GStreamer.
- `rtsp_record.py`: record the main stream locally.
- `rtsp_with_control.py`: gimbal control alongside live video.

All RTSP examples use `rtsp://192.168.144.25:8554/main.264`.
