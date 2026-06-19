#!/usr/bin/env python3
"""Gradio launcher for F5-TTS Russian model."""
import json, os, sys, importlib

CKPT = "/home/drynw/models/f5-tts/F5TTS_v1_Base_v2/model_last_inference.safetensors"
VOCAB = "/home/drynw/models/f5-tts/F5TTS_v1_Base_v2/vocab.txt"
CFG = json.dumps(dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4))

import cached_path as cp
orig = cp.cached_path
cp.cached_path = lambda p, *a, **kw: CKPT if "model_1250000" in str(p) else orig(p, *a, **kw)

os.environ["F5TTS_CKPT"] = CKPT
os.environ["F5TTS_VOCAB"] = VOCAB
os.environ["F5TTS_CFG"] = CFG

from f5_tts.infer.infer_gradio import app, main

import click
sys.argv = [sys.argv[0]]

@click.command()
@click.option("--port", "-p", default=7860)
@click.option("--host", "-H", default="0.0.0.0")
@click.option("--share", "-s", is_flag=True, default=False)
@click.option("--api", "-a", is_flag=True, default=True)
@click.option("--root_path", "-r", default=None)
@click.option("--inbrowser", "-i", is_flag=True, default=False)
def launch(port, host, share, api, root_path, inbrowser):
    app.queue(api_open=api).launch(
        server_name=host, server_port=port, share=share,
        root_path=root_path, inbrowser=inbrowser,
    )

if __name__ == "__main__":
    launch()
