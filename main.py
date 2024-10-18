import os
import shutil
from picamera2 import Picamera2
import signal
import time
from wifi import Cell, Scheme
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont
import Adafruit_SSD1306
from webdav3.client import Client
import smbus  # For I2C communication with PiSugar3

# The To-Do list:
# - Add PiSugar3 support
#   - (test) Add battery % to OLED display
#   - (test) Add shutdown when battery is low (stop recording, cancel in-progress uploads, and shutdown)
#   - (test) Add i2c comms to detect if we are on battery power or plugged in
#     - When WiFi is connected and we go to battery power, that means the car was shut off and we can stop recording and upload the video (and any previous video still pending upload), then shut down
#     - If we go to battery power and don't have wifi, that means the car was shut off away from home, stop recording and shut down (keep video to upload later)
#   - Integrate PiSugar3 RTC to get accurate time on pi for video timestamps
# - Display number of videos pending upload on OLED display
# - Maybe detect speed or at least overlay time on video feed

# ---OLED display imports and setup---
# Raspberry Pi pin configuration:
RST = None  # on the PiOLED this pin isn't used

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
image = Image.new("1", (width, height))
draw = ImageDraw.Draw(image)

# Load default font.
font = ImageFont.load_default()

# PiSugar3 I2C address and registers
I2C_ADDR = 0x75
CHARGE_PERCENTAGE_REG = 0x02
CHARGING_STATUS_REG = 0x03

# I2C bus initialization
bus = smbus.SMBus(1)  # Using I2C bus 1


# Function to get battery percentage from PiSugar3
def get_battery_percentage():
    try:
        percentage = bus.read_byte_data(I2C_ADDR, CHARGE_PERCENTAGE_REG)
        return percentage
    except Exception as e:
        print("Error reading battery percentage:", e)
        return None


# Function to check if the device is plugged in
def is_plugged_in():
    try:
        status = bus.read_byte_data(I2C_ADDR, CHARGING_STATUS_REG)
        # Charging status: 0x00 = not charging, 0x01 = charging
        return status == 0x01
    except Exception as e:
        print("Error reading charging status:", e)
        return None


# Function to display message on OLED display with battery info
def display_message(message):
    draw.rectangle((0, 0, width, height), outline=0, fill=0)  # Clear the display
    draw.text((0, 0), message, font=font, fill=255)  # Draw the message

    # Get battery info
    battery_percentage = get_battery_percentage()
    plugged_in = is_plugged_in()

    # Display battery info
    if battery_percentage is not None and plugged_in is not None:
        charging_status = "Charging" if plugged_in else "On Battery"
        battery_status = f"Battery: {battery_percentage}%\n{charging_status}"
        draw.text((0, height - 16), battery_status, font=font, fill=255)

    disp.image(image)
    disp.display()


# A flag to control whether recording should continue
keep_recording = True

# A flag to check if WiFi connection was previously lost
wifi_lost = False

# Flag to force kill program after 2 signal interrupts
force_quit = False

# WiFi details
ssid = ""

# Configuration for the WebDAV client
options = {"webdav_hostname": "", "webdav_login": "", "webdav_password": ""}

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
        print("Force quit signal received, bailing...")
        exit(0)
    else:
        force_quit = True


# Check if connected to the specified WiFi
def connected_to_wifi():
    cells = Cell.all("wlan0")
    for cell in cells:
        if cell.ssid == ssid:
            return True
    return False


picam2 = Picamera2()

print("Starting video recording...")
display_message("Starting video recording...")
# Start the video recording
picam2.start_and_record_video(video_file)

# Register the function to run on SIGINT
signal.signal(signal.SIGINT, stop_recording)

# Keep the script running while recording should continue
while keep_recording:
    # Calculate the video length and display it
    video_length = datetime.now() - start_time
    message = "Recording...\nLength: " + str(timedelta(seconds=video_length.seconds))
    display_message(message)

    # Check battery status
    battery_percentage = get_battery_percentage()
    plugged_in = is_plugged_in()

    if battery_percentage is not None and plugged_in is not None:
        if not plugged_in:
            if battery_percentage < 20:
                print("Battery low. Stopping recording and shutting down...")
                display_message("Battery low.\nShutting down...")
                keep_recording = False
        else:
            print("Device is plugged in.")
    else:
        print("Could not read battery status.")

    if connected_to_wifi():
        print("Connected to WiFi...")
        # If connected to home WiFi and WiFi connection was previously lost, stop recording
        if wifi_lost:
            print("Reconnected to home WiFi. Signaling stop...")
            display_message("Reconnected to WiFi.\nSignaling stop...")
            keep_recording = False
            print("Waiting for FFmpeg to finish processing frames...")
            time.sleep(5)  # Wait before actually stopping the recording
    else:
        print("Not connected to WiFi...")
        # If not connected to home WiFi, set wifi_lost flag to True
        wifi_lost = True

    # Wait a bit before checking the status again
    time.sleep(1)

# Stop the recording when keep_recording is False
print("Stopping video recording...")
display_message("Stopping video recording...")

# Attempt to stop recording
try:
    picam2.stop_recording()
except:
    time.sleep(5)
    try:
        picam2.stop_recording()
    except:
        pass

# Rename the video file with the end time appended
end_time = datetime.now()
new_file_name = (
    start_time.strftime("%Y%m%d-%H%M%S")
    + "_TO_"
    + end_time.strftime("%Y%m%d-%H%M%S")
    + ".mp4"
)
os.rename(video_file, new_file_name)

# Wait until WiFi is connected to upload the file
while not connected_to_wifi():
    print("Waiting for WiFi connection to upload files...")
    display_message("Waiting for WiFi\nto upload files...")
    time.sleep(1)

print("Opening WebDAV connection...")
display_message("Opening WebDAV\nconnection...")

# Create a WebDAV client
client = Client(options)

for file in os.listdir("."):
    if file.endswith(".mp4"):
        print("Uploading " + file + "...")
        display_message("Uploading\n" + file + "...")
        # Upload the file to the WebDAV server
        client.upload_sync(remote_path="/" + file, local_path=file)
        print("Upload complete.")
        display_message("Upload\ncomplete.")
        # Delete the local file
        print("Deleting local file...")
        display_message("Deleting local file...")
        os.remove(file)
        print("Local file deleted.")
        display_message("Local file deleted.")

# Shutdown the Pi
print("Shutting down...")
display_message("Shutting down...")
# os.system("sudo shutdown now")

exit(0)
