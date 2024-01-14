import os
import shutil
from picamera2 import Picamera2
import signal
import time
from wifi import Cell, Scheme
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
import Adafruit_SSD1306
from datetime import timedelta
from webdav3.client import Client

# ---OLED display imports and setup---
# Raspberry Pi pin configuration:
RST = None     # on the PiOLED this pin isnt used

# 128x64 display with hardware I2C:
disp = Adafruit_SSD1306.SSD1306_128_64(rst=RST)

# Initialize library.
disp.begin()

# Clear display.
disp.clear()
disp.display()

# Create blank image for drawing.
width = disp.width
height = disp.height
image = Image.new('1', (width, height))
draw = ImageDraw.Draw(image)

# Load default font.
font = ImageFont.load_default()

# Function to display message on OLED display
def display_message(message):
    draw.rectangle((0, 0, width, height), outline=0, fill=0)  # clear the display
    draw.text((0, 0), message, font=font, fill=255)  # draw the message
    disp.image(image)
    disp.display()

display_message("We get worse ....")
# ---End OLED display imports and setup---

# A flag to control whether recording should continue
keep_recording = True

# A flag to check if WiFi connection was previously lost
wifi_lost = False

# Flag to force kill program after 2 signal interrupts
force_quit = False

# WiFi details
ssid = ""

# Configuration for the WebDAV client
options = {
    'webdav_hostname': "",
    'webdav_login': "",
    'webdav_password': ""
}

# Video file details
# Get the start date and time
start_time = datetime.now()
# Format it as part of the filename
video_file = start_time.strftime("%Y%m%d-%H%M%S") + ".mp4"

# Define a function to run when SIGINT (Ctrl+C) is received
def stop_recording(signal, frame):
    global keep_recording, force_quit
    keep_recording = False
    if force_quit:
        print('Force quit signal received, bailing...')
        # kill the script
        exit(0)
    else:
        force_quit = True

# Check if connected to the specified WiFi
def connected_to_wifi():
    cells = Cell.all('wlan0')
    for cell in cells:
        if cell.ssid == ssid:
            return True
    return False

picam2 = Picamera2()

print('Starting video recording...')
display_message("Starting video recording...")
# Start the video recording
picam2.start_and_record_video(video_file)

# Register the function to run on SIGINT
signal.signal(signal.SIGINT, stop_recording)

# Keep the script running while recording should continue
while keep_recording:
    # Calculate the video length and display it
    video_length = datetime.now() - start_time
    display_message("Recording...\nLength: " + str(timedelta(seconds=video_length.seconds)))
    
    if connected_to_wifi():
        print('Connected to WiFi...')
        # If connected to home WiFi and WiFi connection was previously lost, stop recording
        if wifi_lost:
            print('Reconnected to home WiFi,. Signaling stop...')
            display_message("Reconnected to home WiFi, signaling stop...")
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
display_message("Stopping video recording...")

# if this fails, try it again
try:
  picam2.stop_recording()
except:
  # wait a bit and try again
  time.sleep(5)
  try:
    picam2.stop_recording()
  except:
      # continue on
      pass

# Rename the video file with the end time appended
end_time = datetime.now()
new_file_name = start_time.strftime("%Y%m%d-%H%M%S") + "_TO_" + end_time.strftime("%Y%m%d-%H%M%S") + ".mp4"
os.rename(video_file, new_file_name)

# Wait until WiFi is connected to mount the network drive and copy the file
while not connected_to_wifi():
    print('Waiting for WiFi connection to mount network drive...')
    display_message("Waiting for WiFi\nto mount\nnetwork drive...")
    time.sleep(1)

# For all .mp4 files in the current directory, send their asses to the WebDAV server
print('Opening webdav connection...')
display_message("Opening webdav\nconnection...")

# Create a WebDAV client
client = Client(options)

for file in os.listdir("."):
    if file.endswith(".mp4"):
        print('Uploading ' + file + '...')
        display_message("Uploading\n" + file + "...")
        # Upload the file to the WebDAV server
        client.upload_sync(remote_path="/"+file, local_path=file)
        print('Upload complete.')
        display_message("Upload\ncomplete.")
        # Delete the local file
        print('Deleting local file...')
        display_message("Deleting local file...")
        os.remove(file)
        print('Local file deleted.')
        display_message("Local file deleted.")

# Shutdown the Pi
print('Shutting down...')
display_message("Shutting down...")
#os.system("sudo shutdown now")

# kill the script
exit(0)
