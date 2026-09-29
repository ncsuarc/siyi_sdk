# A8 Mini SDK protocol notes

The [official SIYI protocol](https://siyi.biz/siyi_file/A8%20mini/SIYI_Gimbal_Camera_External_SDK_Protocol_Update_Log%20V0.1.1.pdf) and [A8 Mini manual](https://siyi.biz/siyi_file/A8%20mini/A8%20mini%20User%20Manual%20v1.6.pdf) describe the wire format and model support. A [repository copy](../SIYI_SDK_PROTOCOL.pdf) is also available. The Python package includes:

- System: firmware, hardware ID, system time, network configuration, and soft reboot
- Gimbal: rotation, centering, target angles, mode, attitude, and magnetic encoder
- Camera: photo/video, digital zoom, encoding, SD card, picture naming, HDMI/CVBS output, and OSD
- Integration: aircraft attitude, raw GPS, flight-controller and gimbal streams, and ArduPilot diagnostics

Control uses UDP or TCP on port `37260`, or UART at the configured baud rate. TCP connections use a 1 Hz heartbeat. The protocol calls `SEQ` a frame sequence; it does not require ACKs to copy the request sequence. A8 Mini UDP replies observed here used a different counter, so the SDK matches replies by command ID by default. Strict sequence matching remains opt-in. The protocol also says A8 Mini single-axis command `0x41` replies under command ID `0x0E`.

The protocol's support appendix lists picture-naming commands `0x49`/`0x4A` and several gimbal diagnostics for other models, without an A8 Mini checkmark. Those methods remain in the SDK for now under the earlier retained-command scope, but their A8 Mini behavior is unverified. The appendix says A8 Mini gives no response to SD formatting (`0x48`), while the older A8 Mini manual defines an ACK. A timeout after a format request is therefore **unconfirmed**, not proof of success or failure.

The A8 Mini has no thermal sensor, laser rangefinder, or optical focus motor. AI tracking requires the optional SIYI AI Module II and is outside this SDK. Video stitching is for multi-sensor cameras. The A8 Mini RTSP stream is `rtsp://192.168.144.25:8554/main.264`.

The camera status response contains some vendor-wide fields, such as laser and HDR status. The parser retains those wire fields so packet offsets and decoding stay correct; they do not imply corresponding A8 Mini controls.
