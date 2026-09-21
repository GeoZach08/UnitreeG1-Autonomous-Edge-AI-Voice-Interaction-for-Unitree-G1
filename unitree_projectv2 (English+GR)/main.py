import cv2
import time
import sys
import os
import audioop
import unicodedata
import threading
import speech_recognition as sr
import warnings
import subprocess
import re
import pyaudio
from ctypes import *
from ultralytics import YOLO

warnings.filterwarnings("ignore")

import config
from llm import ask_llm

# Force PulseAudio defaults for the Jetson environment.
# Sometimes Linux defaults to a dummy audio device or a different port on boot.
# These commands ensure the OS routes everything through the robot's actual hardware.
os.system("pactl set-default-sink g1_speaker")
os.system("pactl set-sink-mute g1_speaker 0")
os.system("pactl set-sink-volume g1_speaker 100%")
os.system("pactl set-default-source g1_microphone")

# SDL needs to know we are using PulseAudio, otherwise pygame/ffplay might fail
os.environ["SDL_AUDIODRIVER"] = "pulseaudio"

if config.USE_REAL_ROBOT:
    import robot_control
    robot_control.init()

is_speaking = False
state_lock = threading.Lock()

def change_led(color: str):
    # Wrapper to handle LED changes transparently whether we are testing locally or on the robot
    if config.USE_REAL_ROBOT:
        robot_control.set_led(color)
    else:
        print(f"[MOCK] LED changed to: {color.upper()}")

def mock_speak(text: str):
    # Local fallback for generating and playing speech when the robot SDK isn't available
    mp3_filename = "mock_babis.mp3"
    
    cleaned_text = text.replace('-', ' ').replace('*', '').replace('#', '').replace('"', '').replace("'", "")
    if not cleaned_text.strip(): 
        cleaned_text = "Συγγνώμη, δεν το κατάλαβα."
    
    # Dynamic language routing: switch to English TTS if latin characters are detected
    is_english = bool(re.search(r'[a-zA-Z]', cleaned_text))
    voice = "en-US-ChristopherNeural" if is_english else "el-GR-NestorasNeural"
    
    try:
        subprocess.run(["edge-tts", "--voice", voice, "--text", cleaned_text, "--volume=+10%", "--write-media", mp3_filename], check=True, timeout=30)
        if os.path.exists(mp3_filename) and os.path.getsize(mp3_filename) > 0:
            play_env = os.environ.copy()
            play_env["PULSE_SINK"] = "g1_speaker"
            subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", mp3_filename], check=True, env=play_env)
    except Exception as e:
        print(f"Mock audio error: {e}")
    finally:
        if os.path.exists(mp3_filename):
            try: os.remove(mp3_filename)
            except: pass

def perform_greet():
    # Spawns the greeting in a separate thread so the main camera loop doesn't freeze
    global is_speaking
    try:
        print(f"\n[{time.strftime('%H:%M:%S')}] >>> GREETING TRIGGERED <<<")
        if config.USE_REAL_ROBOT:
            threading.Thread(target=robot_control.wave, daemon=True).start()
            robot_control.speak_edge(config.GREETING_PHRASE) 
        else:
            mock_speak(config.GREETING_PHRASE)
    except Exception as e:
        print(f"Greet error: {e}")
    finally:
        with state_lock:
            is_speaking = False

print("Initializing audio pipeline...")

# Suppress annoying ALSA underrun/overrun C-level error logs that flood the terminal
try:
    ERROR_HANDLER_FUNC = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)
    def py_error_handler(filename, line, function, err, fmt): pass
    c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)
    asound = cdll.LoadLibrary('libasound.so')
    asound.snd_lib_error_set_handler(c_error_handler)
except:
    pass

def get_pulse_mic_index():
    # Dynamically find the PulseAudio virtual mic index instead of hardcoding it,
    # as device indexes can shift on Linux across reboots.
    target_idx = config.MIC_INDEX
    try:
        p = pyaudio.PyAudio()
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if "pulse" in info["name"].lower() or "default" in info["name"].lower():
                target_idx = i
                break
        p.terminate()
    except:
        pass
    return target_idx

r = sr.Recognizer()
pulse_idx = get_pulse_mic_index()
mic = sr.Microphone(device_index=pulse_idx)

WAKE_WORDS = ["μπαμπ", "barbie", "μπαρμπι"]
AUTO_QUESTION_TIMEOUT = 12
MAX_CONSECUTIVE_AUTO_QUESTIONS = 6
MIN_RMS_FOR_WAKE_CHECK = 100

def strip_accents(text):
    normalized = unicodedata.normalize("NFD", text)
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn")

def transcribe_audio(audio_data):
    try:
        return r.recognize_google(audio_data, language="el-GR").strip()
    except sr.UnknownValueError:
        return ""
    except Exception as e:
        print(f"STT API error: {e}")
        return ""

print("Loading YOLOv8-nano...")
model_vision = YOLO("yolov8n.pt")
PERSON_CLASS_ID = 0
MIN_BOX_HEIGHT_RATIO = 0.35

def person_is_close(frame):
    # Detects if a person is occupying a significant portion of the frame vertically
    results = model_vision(frame, verbose=False)[0]
    frame_height = frame.shape[0]
    close_person = False
    
    for box in results.boxes:
        if int(box.cls[0]) != PERSON_CLASS_ID: 
            continue
            
        x1, y1, x2, y2 = map(int, box.xyxy[0])
        is_close = ((y2 - y1) / frame_height) >= MIN_BOX_HEIGHT_RATIO
        
        # Draw bounding boxes for debugging
        color, thickness = ((0, 255, 0), 2) if is_close else ((128, 128, 128), 1)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, thickness)
        
        if is_close: 
            close_person = True
            
    return close_person

def start_listening(callback):
    # Setup background listening thread with dynamic thresholding disabled for stability
    with mic as source:
        r.energy_threshold = 300
        r.dynamic_energy_threshold = False
    return r.listen_in_background(mic, callback, phrase_time_limit=10)

def run_conversation_session(cap, last_person_seen):
    # Finite State Machine managing the transition between active conversation and wake-word standby
    global is_speaking
    mode = "auto"
    consecutive_auto_questions = 0
    last_activity_time = time.time()

    change_led("GREEN")

    def process_and_answer(audio_data):
        # Handle the LLM request and TTS playback in a detached thread
        global is_speaking
        nonlocal consecutive_auto_questions, last_activity_time

        text = transcribe_audio(audio_data)
        if not text:
            with state_lock:
                is_speaking = False
            return

        print(f"Heard: {text}")
        change_led("PURPLE")
        
        answer = ask_llm(text)
        print(f"LLM Reply: {answer}")

        try:
            if config.USE_REAL_ROBOT:
                robot_control.speak_edge(answer)
            else:
                mock_speak(answer)
        except Exception as e:
            print(f"Playback error: {e}")
        finally:
            with state_lock:
                consecutive_auto_questions += 1
                last_activity_time = time.time()
                is_speaking = False
            
            change_led("GREEN")

    def unified_callback(recognizer, audio_data):
        # Callback triggered by speech_recognition when it detects audio chunks
        global is_speaking
        nonlocal mode, last_activity_time, consecutive_auto_questions

        if is_speaking: 
            return

        # Fast discard of low-volume noise before hitting the Google API
        rms = audioop.rms(audio_data.get_raw_data(), audio_data.sample_width)
        if rms < MIN_RMS_FOR_WAKE_CHECK: 
            return

        with state_lock: 
            current_mode = mode

        if current_mode == "auto":
            with state_lock: 
                is_speaking = True
            threading.Thread(target=process_and_answer, args=(audio_data,), daemon=True).start()

        elif current_mode == "wake":
            heard = transcribe_audio(audio_data)
            if heard: 
                print(f"[wake-word check] Detected: {heard}")
            # Relaxed matching to catch mispronunciations of the wake word
            if any(word in strip_accents(heard.lower()) for word in WAKE_WORDS):
                print("Wake word detected!")
                with state_lock:
                    mode = "auto"
                    consecutive_auto_questions = 0
                    last_activity_time = time.time()
                change_led("GREEN")

    stop_listening = start_listening(unified_callback)

    try:
        while True:
            ret, frame = cap.read()
            if not ret: 
                continue

            with state_lock:
                current_mode = mode
                currently_speaking = is_speaking
                current_last_activity = last_activity_time
                current_consec_qs = consecutive_auto_questions

            idle_time = time.time() - current_last_activity
            reached_max = current_consec_qs >= MAX_CONSECUTIVE_AUTO_QUESTIONS

            # Keep the session alive if someone is in frame or the robot is still talking
            if person_is_close(frame) or currently_speaking:
                last_person_seen = time.time()
            elif time.time() - last_person_seen > reset_timeout:
                print(f"Session reset after {reset_timeout}s of absence.")
                change_led("BLUE") 
                stop_listening(wait_for_stop=False)
                return "absence_reset"
            
            label = "Active (Auto)" if current_mode == "auto" else "Standby (Wake Word)"
            cv2.putText(frame, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 200, 0), 2)
            
            if not config.HEADLESS_MODE:
                cv2.imshow("Jetson AI Pipeline", frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    stop_listening(wait_for_stop=False)
                    cap.release()
                    cv2.destroyAllWindows()
                    sys.exit(0)
            else:
                time.sleep(0.03) # Cap loop speed in headless mode to reduce CPU usage

            # Drop back to wake-word mode if user is quiet for too long or asked too many questions
            if current_mode == "auto" and not currently_speaking and (idle_time > AUTO_QUESTION_TIMEOUT or reached_max):
                with state_lock:
                    mode = "wake"
                    last_activity_time = time.time()
                change_led("BLUE")

    finally:
        stop_listening(wait_for_stop=False)

# Main entry point
cap = cv2.VideoCapture(config.CAMERA_INDEX, cv2.CAP_V4L2)
has_greeted = False
last_time_seen = time.time()
reset_timeout = 15

print("\nStarting brain core...")
change_led("BLUE")

while True:
    ret, frame = cap.read()
    if not ret: 
        break

    current_time = time.time()
    
    # Global reset if user leaves the frame entirely
    if current_time - last_time_seen > reset_timeout:
        if has_greeted:
            has_greeted = False
            change_led("BLUE") 

    if person_is_close(frame):
        last_time_seen = time.time()

        if not has_greeted:
            change_led("PURPLE")
            
            with state_lock:
                is_speaking = True
            
            threading.Thread(target=perform_greet, daemon=True).start()
            
            # Keep pumping the camera frames to prevent OpenCV buffer build-up during speech
            while True:
                ret, wait_frame = cap.read()
                if ret:
                    if not config.HEADLESS_MODE:
                        cv2.imshow("Jetson AI Pipeline", wait_frame)
                        cv2.waitKey(1)
                    else:
                        time.sleep(0.03)
                
                with state_lock:
                    if not is_speaking:
                        break

            has_greeted = True
            session_result = run_conversation_session(cap, last_time_seen)

            if session_result == "absence_reset":
                has_greeted = False
            else:
                last_time_seen = time.time()

    if not config.HEADLESS_MODE:
        cv2.imshow("Jetson AI Pipeline", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
    else:
        time.sleep(0.03)

cap.release()
if not config.HEADLESS_MODE:
    cv2.destroyAllWindows()