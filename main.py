import os
import shutil
from picamera2 import Picamera2
import signal
import time
from wifi import Cell, Scheme
from datetime import datetime

# A flag to control whether recording should continue
keep_recording = True

# A flag to check if WiFi connection was previously lost
wifi_lost = False

# WiFi details
ssid = "WiFi Name Here"

# Network storage details
server_ip = "192.168.1.2"
shared_folder = "mysharedfolder"
mount_point = "/mnt/myfolder"
username = "yourusername"
password = "yourpassword"

# Video file details
# Get the start date and time
start_time = datetime.now()
# Format it as part of the filename
video_file = start_time.strftime("%Y%m%d-%H%M%S") + ".mp4"

# Define a function to run when SIGINT (Ctrl+C) is received
def stop_recording(signal, frame):
    global keep_recording
    keep_recording = False

# Check if connected to the specified WiFi
def connected_to_wifi():
    cells = Cell.all('wlan0')
    for cell in cells:
        if cell.ssid == ssid:
            return True
    return False

# Mount the network drive, this will probably require sudo when running this program
def mount_network_drive():
    print('Mounting network drive...')
    os.system(f"sudo mount -t cifs //{server_ip}/{shared_folder} {mount_point} -o username={username},password={password},iocharset=utf8,file_mode=0777,dir_mode=0777")
    print('Network drive mounted.')

picam2 = Picamera2()

print('Starting video recording...')
# Start the video recording
picam2.start_and_record_video(video_file)

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

# Rename the video file with the end time appended
end_time = datetime.now()
new_file_name = start_time.strftime("%Y%m%d-%H%M%S") + "_TO_" + end_time.strftime("%Y%m%d-%H%M%S") + ".mp4"
os.rename(video_file, new_file_name)

# Wait until WiFi is connected to mount the network drive and copy the file
while not connected_to_wifi():
    print('Waiting for WiFi connection...')
    time.sleep(1)

# Mount the network drive
mount_network_drive()

# Copy the file to network storage
print('Copying file to network storage...')
shutil.copy2(video_file, mount_point)
print('File copied to network storage.')

# Verify that the file was copied successfully to the network storage then make sure the size of the local file and network file are the same
print('Verifying remote file...')
if os.path.isfile(f"{mount_point}/{video_file}") and os.path.getsize(video_file) == os.path.getsize(f"{mount_point}/{video_file}"):
    print('Remote file verified.')
    # Delete the local file
    print('Deleting local file...')
    os.remove(video_file)
    print('Local file deleted.')
else:
    print('File not found in network storage, keeping local copy.')
    print('Unmounting network drive...')
    os.system(f"sudo umount {mount_point}")
    print('Network drive unmounted.')

# Shutdown the Pi
print('Shutting down...')
os.system("sudo shutdown now")

# kill the script
exit(0)
