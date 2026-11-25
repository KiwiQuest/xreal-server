@echo off
python -u XREAL_MOUSE.py 2>&1 | powershell -Command "$input | Tee-Object -FilePath log.txt"
