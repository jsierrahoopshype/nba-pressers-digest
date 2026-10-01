# Face detection model

`face_detection_yunet_2023mar.onnx` is OpenCV's YuNet face detector, copied
unchanged from https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet
(MIT License, Copyright (c) 2020 Shiqi Yu). `reframe.py` uses it to keep the
speaker in frame in the vertical and square clips. The clip tool's installer
and self-update download it from here and check its SHA-256:
`8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`.
