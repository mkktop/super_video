---
name: embedded-dev-jlink-context
description: 用户在 super_video 之外兼做嵌入式开发——2026-08-30 查过本机串口与 J-Link VCOM，当时探针未接
metadata:
  node_type: memory
  type: user
  originSessionId: sess_b16617ae-8b16-4da4-b7dd-da7f25ce3049
---

2026-08-30 用户主动查本机串口（当时仅蓝牙虚拟 COM3/COM4，无任何 USB 转串口）并追问「没有 J-Link 的串口吗」——USB 设备列表里无 SEGGER 设备（VID 1366），探针当时没接，但用户应有 J-Link 且关心其虚拟串口（VCOM）。说明用户除视频超分项目外还做嵌入式/MCU 开发，嵌入式工具链（调试器、串口）的问题可直接给专业级回答，不必从零科普。相关环境：本机代理与 localhost 坑见 [[local-proxy-hijacks-localhost]]。
