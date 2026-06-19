#!/bin/bash
source ~/f5-tts-env/bin/activate
exec python3 /home/drynw/models/f5-tts/run_gradio.py "$@"
