import os
import shutil
from picamera2 import Picamera2
import signal
import time
from wifi import Cell
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont
import adafruit_ssd1306
from board import SCL, SDA
import busio
from webdav3.client import Client
import smbus  # For I2C communication with PiSugar3
import urllib3

###############################################
#      General Setup and Helpers (Wi-Fi)               
###############################################
# region general

# WiFi SSID
ssid = "Steffen IoT"

def connected_to_wifi():
    cells = Cell.all("wlan0")
    for cell in cells:
        if cell.ssid == ssid:
            return True
    return False

# For LAN WebDav, ignore self-signed SSL. Remove this if using public server!
urllib3.disable_warnings()

# endregion

###############################################
#       OLED Display Setup and Helpers               
###############################################
# region display

# ---OLED display imports and setup---
# Create the I2C interface.
i2c = busio.I2C(SCL, SDA)

# Create the SSD1306 OLED object for a 128x64 pixel display
disp = adafruit_ssd1306.SSD1306_I2C(128, 64, i2c, addr=0x3C)

# Clear display.
disp.fill(0)
disp.show()

# Create blank image for drawing.
width = disp.width
height = disp.height
image = Image.new("1", (width, height))
draw = ImageDraw.Draw(image)

# Load default font.
font = ImageFont.load_default()

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
    disp.show()

# endregion

###############################################
#         PiSugar3 Setup and Helpers               
###############################################
# region pisugar3

# PiSugar3 I2C addresses and registers
I2C_BATTERY_ADDR = 0x57  # Address for PiSugar3
BATTERY_PERCENTAGE_REG = 0x2A  # Register for battery percentage
POWER_STATUS_REG = 0x02  # Register for power status (7th bit for external power)

# I2C bus initialization
bus = smbus.SMBus(1)  # Using I2C bus 1

# Function to get battery percentage from PiSugar3
def get_battery_percentage():
    try:
        percentage = bus.read_byte_data(I2C_BATTERY_ADDR, BATTERY_PERCENTAGE_REG)
        return percentage
    except Exception as e:
        print("Error reading battery percentage:", e)
        return None

# Function to check if the device is plugged in (external power detection)
def is_plugged_in():
    try:
        power_status = bus.read_byte_data(I2C_BATTERY_ADDR, POWER_STATUS_REG)
        # Check the 7th bit (0x80) for external power status: 1 = plugged in, 0 = not plugged in
        return bool(power_status & 0x80)
    except Exception as e:
        print("Error reading charging status:", e)
        return None

# endregion

###############################################
#          WebDav3 Setup and Helpers               
###############################################
# region webdav

# WebDAV client options
options = {
    'webdav_hostname': "https://192.168.3.6/remote.php/dav/files/logan/dashcams/logan/",
    'webdav_login': "logan",
    'webdav_password': "8ZXXC-tiH52-AnDqR-bqmXY-s9gSx"  # App password
}

# Initialize WebDAV client
client = Client(options)
client.verify = False

def upload_files():
    print("Opening WebDAV connection...")
    display_message("Opening WebDAV\nconnection...")
    
    for file in sorted(os.listdir(".")):
        if file.endswith(".mp4"):
            print("Uploading:", file)
            display_message(f"Uploading\n{file}...")
            retries = 3
            while retries > 0:
                try:
                    client.upload_sync(remote_path="/" + file, local_path=file)
                    print("Upload complete:", file)
                    display_message("Upload complete.")
                    os.remove(file)
                    break  # Exit retry loop on success
                except Exception as e:
                    print(f"Failed to upload {file}. Error:", e)
                    retries -= 1
                    if retries == 0:
                        print("Upload failed after 3 attempts. Keeping file for next time.")
                        return False
                    time.sleep(5)  # Delay before retrying
    return True

# endregion

###############################################
#          Camera Setup and Control               
###############################################
# region camera

# Initialize Picamera2
picam2 = Picamera2()

def start_recording():
    start_time = datetime.now()
    video_file = start_time.strftime("%Y%m%d-%H%M%S") + ".mp4"
    print("Starting video recording:", video_file)
    display_message("Starting video recording...")
    picam2.start_and_record_video(video_file)
    return video_file, start_time

def stop_recording(video_file, start_time):
    try:
        picam2.stop_recording()
    except Exception:
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
    print("Recording stopped. File saved as:", new_file_name)
    return new_file_name

# endregion


###############################################
#    --------------- MAIN ---------------              
###############################################

# Main logic
def main():
    signal.signal(signal.SIGINT, lambda s, f: exit(0))

    # Start recording immediately on startup
    video_file, start_time = start_recording()

    # Continuously monitor power and WiFi status
    while is_plugged_in():
        display_message(f"Recording...\n{video_file}")
        time.sleep(1)

    # Power was lost; stop recording
    new_file_name = stop_recording(video_file, start_time)

    # Check WiFi connection and try to upload if connected
    if connected_to_wifi():
        success = upload_files()
        if success:
            print("All files uploaded. Shutting down.")
        else:
            print("Upload failed. Files will be kept for next time.")
    else:
        print("Not connected to WiFi. Saving files for later upload.")

    # Shut down after completion
    display_message("Shutting down...")
    print("Shutting down...")
    os.system("sudo shutdown now")

if __name__ == "__main__":
    main()
