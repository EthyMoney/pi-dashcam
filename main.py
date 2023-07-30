from picamera2 import Picamera2
import signal
import time
from wifi import Cell, Scheme

# A flag to control whether recording should continue
keep_recording = True

# A flag to check if WiFi connection was previously lost
wifi_lost = False

# WiFi details
ssid = "REPLACE ME"

# Define a function to run when SIGINT (Ctrl+C) is received
def stop_recording(signal, frame):
    global keep_recording
    keep_recording = False
    print('Got signal to stop recording (Ctrl+C)')

# Check if connected to the specified WiFi
def connected_to_wifi():
    cells = Cell.all('wlan0')
    for cell in cells:
        if cell.ssid == ssid:
            return True
    return False

picam2 = Picamera2()
video_config = picam2.create_video_configuration(main={"size": (1920, 1080)})
picam2.configure(video_config)

print('Starting video recording...')
# Start the video recording
picam2.start_and_record_video("test.mp4")

# Register the function to run on SIGINT
signal.signal(signal.SIGINT, stop_recording)

# Keep the script running while recording should continue
while keep_recording:
    if connected_to_wifi():
        print('Connected to WiFi...')
        # If connected to home WiFi and WiFi connection was previously lost, stop recording
        if wifi_lost:
            print('Previously lost WiFi connection. Stopping recording...')
            keep_recording = False
            print('Waiting for FFmpeg to finish processing frames...')
            time.sleep(5)  # Wait before actually stopping the recording
    else:
        print('Not connected to WiFi...')
        # If not connected to home WiFi, set wifi_lost flag to True
        wifi_lost = True

    # Wait a bit before checking the WiFi status again
    time.sleep(1)

# Stop the recording when keep_recording is False
print('Stopping video recording...')
picam2.stop_recording()
