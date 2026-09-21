"""
Handles SDK interactions for the Unitree G1.
Contains a keep-alive mechanism to prevent the native firmware from overriding custom LED colors.
"""

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.g1.arm.g1_arm_action_client import G1ArmActionClient
from unitree_sdk2py.g1.arm.g1_arm_action_client import action_map
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

import os
import subprocess
import time
import threading
import config
import re

_initialized = False
_arm_client = None
_audio_client = None

_current_led_color = "blue"
_led_lock = threading.Lock()

def _led_keepalive():
    # The G1's internal state machine frequently attempts to reset the face LEDs.
    # We run this loop to aggressively re-apply our selected state and overwrite the firmware's commands.
    global _current_led_color
    while True:
        with _led_lock:
            color = _current_led_color
        
        if color == "purple":
            r, g, b = 128, 0, 128   # Talking / Greeting
        elif color == "green":
            r, g, b = 0, 255, 0     # Active listening
        elif color == "blue":
            r, g, b = 0, 0, 255     # Standby
        else:
            r, g, b = 255, 255, 255 
            
        try:
            if _audio_client is not None:
                _audio_client.LedControl(r, g, b)
        except Exception:
            pass
        
        # Pushing updates every 0.5s is enough to win the race condition without flooding the bus
        time.sleep(0.5)

def init():
    # Sets up the UDP channels and initializes action clients
    global _initialized, _arm_client, _audio_client
    if _initialized:
        return
    
    ChannelFactoryInitialize(0, config.ROBOT_NETWORK_INTERFACE)
    
    _arm_client = G1ArmActionClient()
    _arm_client.SetTimeout(10.0)
    _arm_client.Init()

    _audio_client = AudioClient()
    _audio_client.SetTimeout(10.0)
    _audio_client.Init()
    _audio_client.SetVolume(100) 
    
    threading.Thread(target=_led_keepalive, daemon=True).start()
    _initialized = True

def set_led(color: str):
    # Updates the thread-safe state variable, letting the keepalive loop handle the actual hardware call
    global _current_led_color
    if not _initialized:
        return
        
    with _led_lock:
        _current_led_color = color.lower()

def speak_edge(text: str):
    # Cloud TTS pipeline utilizing Microsoft Edge's Neural voices
    if not _initialized:
        raise RuntimeError("robot_control.init() has not been called.")

    mp3_filename = "temp_local_babis.mp3"

    # Pre-processing: Clean up markdown tokens that might leak from the LLM.
    cleaned_text = text.replace('-', ' ').replace('*', '').replace('#', '').replace('"', '').replace("'", "")
    
    if not cleaned_text.strip():
        cleaned_text = "Συγγνώμη, δεν το κατάλαβα αυτό."

    # Dynamic language detection for TTS voice selection based on the presence of Latin characters
    is_english = bool(re.search(r'[a-zA-Z]', cleaned_text))
    voice = "en-US-ChristopherNeural" if is_english else "el-GR-NestorasNeural"

    try:
        subprocess.run(
            [
                "edge-tts",
                "--voice", voice,
                "--text", cleaned_text,
                "--volume=+80%",
                "--write-media", mp3_filename,
            ],
            check=True,
            timeout=30, 
        )
    except subprocess.TimeoutExpired:
        print("TTS Error: Request timed out.")
        return
    except Exception as e:
        print(f"TTS Error: {e}")
        return

    # Routing the generated audio strictly to the robot's hardware sink via PulseAudio env vars
    try:
        if os.path.exists(mp3_filename) and os.path.getsize(mp3_filename) > 0:
            play_env = os.environ.copy()
            play_env["PULSE_SINK"] = "g1_speaker"
            
            subprocess.run(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", mp3_filename],
                check=True,
                env=play_env
            )
    except Exception as e:
        print(f"ffplay execution failed: {e}")
    finally:
        if os.path.exists(mp3_filename):
            try:
                os.remove(mp3_filename)
            except:
                pass

def wave():
    # Triggers a pre-defined animation state on the robot's arms
    if not _initialized:
        raise RuntimeError("robot_control.init() has not been called.")
    try:
        _arm_client.ExecuteAction(action_map.get("high wave"))
    except Exception as e:
        print(f"Arm actuation error: {e}")