"""fast_video.py"""

# External libraries
import cv2
import numpy as np
from scipy.io import wavfile
from audiotsm import phasevocoder

# Internal libraries
import math
import sys
import time
import os
import subprocess
import argparse
from shutil import rmtree
from datetime import timedelta

TEMP_FOLDER = '.TEMP'

class ArrReader:
    pointer = 0

    def __init__(self, arr, channels, samplerate, samplewidth):
        self.samples = arr
        self._channels = channels
        self.samplerate = samplerate
        self.samplewidth = samplewidth

    @property
    def channels(self):
        return self._channels

    @property
    def empty(self):
        return self.samples.shape[0] <= self.pointer

    def read(self, buffer):
        if buffer.shape[0] != self.channels:
            raise ValueError(
                "the buffer should have the same number of " "channels as the ArrReader"
            )

        end = self.pointer + buffer.shape[1]
        frames = self.samples[self.pointer : end].T.astype(np.float32)
        n = frames.shape[1]
        np.copyto(buffer[:, :n], frames)
        del frames
        self.pointer = end
        return n

    def skip(self, n):
        pastPointer = self.pointer
        self.pointer += n
        return self.pointer - pastPointer

    def __enter__(self):
        return self

    def __exit__(self, _1, _2, _3):
        pass


class ArrWriter:
    pointer = 0

    def __init__(self, arr, channels, samplerate, samplewidth):
        self._channels = channels
        self.output = arr

    @property
    def channels(self):
        return self._channels

    def write(self, buffer):
        if buffer.shape[0] != self.channels:
            raise ValueError(
                "the buffer should have the same number of" "channels as the ArrWriter"
            )

        end = self.pointer + buffer.shape[1]
        changedBuffer = buffer.T.astype(np.int16)
        n = buffer.shape[1]
        self.output = np.concatenate((self.output, changedBuffer))
        self.pointer = end
        return n

    def __enter__(self):
        return self

    def __exit__(self, _1, _2, _3):
        pass


parser = argparse.ArgumentParser()
parser.add_argument("videoFile", help="the path to the video file you want modified.")
# parser.add_argument('-v', '--videoSpeed', type=float, default=1.0,
#     help='the speed that the video plays at.')
parser.add_argument("--silentSpeed", "-s", type=float, default=99999,
    help="the speed that silent frames should be played at.")
parser.add_argument("--silentThreshold", "-t", type=float, default=0.04,
    help="the volume that frames audio needs to surpass to be sounded. It ranges from 0 to 1.")
parser.add_argument("--frameMargin", "-m", type=int, default=4,
    help="tells how many frames on either side of speech should be included.")
args = parser.parse_args()

startTime = time.time()

videoFile = args.videoFile
NEW_SPEED = [args.silentSpeed, 1]
silentThreshold = args.silentThreshold
frame_margin = args.frameMargin

cap = cv2.VideoCapture(videoFile)

width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
fourcc = cv2.VideoWriter_fourcc(*"mp4v")
fps = round(cap.get(cv2.CAP_PROP_FPS))

try:
    os.mkdir(TEMP_FOLDER)
except OSError:
    rmtree(TEMP_FOLDER)
    os.mkdir(TEMP_FOLDER)

extractAudio = 'ffmpeg -i "{}" -ab 160k -ac 2 -ar 44100 -vn {}/output.wav'.format(
    videoFile, TEMP_FOLDER
)
subprocess.call(extractAudio, shell=True)

out = cv2.VideoWriter(TEMP_FOLDER + "/spedup.mp4", fourcc, fps, (width, height))
sampleRate, audioData = wavfile.read(TEMP_FOLDER + "/output.wav")

skipped = 0
nFrames = 0
channels = int(audioData.shape[1])


def getMaxVolume(s):
    maxv = np.max(s)
    minv = np.min(s)
    return max(maxv, -minv)


def writeFrames(frames, nAudio, speed, samplePerSecond, writer):
    numAudioChunks = round(nAudio / samplePerSecond * fps)
    global nFrames
    numWrites = numAudioChunks - nFrames
    # a = [1, 2, 3], len(a) == 3 but a[3] is error
    limit = len(frames) - 1
    for i in range(numWrites):
        frameIndex = round(i * speed)
        if frameIndex > limit:
            writer.write(frames[-1])
        else:
            writer.write(frames[frameIndex])
        nFrames += 1


normal = 0
# 0 for silent, 1 for normal
switchStart = 0
maxVolume = getMaxVolume(audioData)

# not used:
# fadeInSamples = 400
# preMask = np.arange(fadeInSamples)/fadeInSamples
# mask = np.repeat(preMask[:, np.newaxis], 2, axis = 1)

y = np.zeros_like(audioData, dtype=np.int16)
yPointer = 0
frameBuffer = []

while cap.isOpened():
    ret, frame = cap.read()
    if not ret:
        break
    # since samplerate is in seconds, I need to convert this to second as well
    currentTime = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
    audioSampleStart = math.floor(currentTime * sampleRate)

    # audioSampleStart + one frame worth of samples
    audioSampleEnd = min(
        (audioSampleStart + ((sampleRate // fps) * frame_margin)), (len(audioData))
    )
    switchEnd = audioSampleStart + ((sampleRate // fps))
    audioChunkMod = audioData[audioSampleStart:switchEnd]
    audioChunk = audioData[audioSampleStart:audioSampleEnd]

    # if it's quite
    if getMaxVolume(audioChunk) / maxVolume < silentThreshold:
        skipped += 1
        # if the frame is 'switched'
        frameBuffer.append(frame)
        normal = 0
    else:  # if it's 'loud'

        # and the last frame is 'loud'
        if normal:
            out.write(frame)
            nFrames += 1
            switchStart = switchEnd

            yPointerEnd = yPointer + audioChunkMod.shape[0]
            y[yPointer:yPointerEnd] = audioChunkMod
            yPointer = yPointerEnd
        else:
            spedChunk = audioData[switchStart:switchEnd]
            spedupAudio = np.zeros((0, 2), dtype=np.int16)
            with ArrReader(spedChunk, channels, sampleRate, 2) as reader:
                with ArrWriter(spedupAudio, channels, sampleRate, 2) as writer:
                    tsm = phasevocoder(reader.channels, speed=NEW_SPEED[normal])
                    tsm.run(reader, writer)
                    spedupAudio = writer.output

            yPointerEnd = yPointer + spedupAudio.shape[0]
            y[yPointer:yPointerEnd] = spedupAudio
            yPointer = yPointerEnd

            writeFrames(frameBuffer, yPointerEnd, NEW_SPEED[normal], sampleRate, out)
            frameBuffer = []
            switchStart = switchEnd

        normal = 1
    if skipped % 500 == 0:
        print("{} frames inspected".format(skipped))
        skipped += 1

y = y[:yPointer]
wavfile.write(TEMP_FOLDER + "/spedupAudio.wav", sampleRate, y)

cap.release()
out.release()
cv2.destroyAllWindows()

outFile = "{}_faster{}".format(
    videoFile[: videoFile.rfind(".")], videoFile[videoFile.rfind(".") :]
)
command = "ffmpeg -y -i {}/spedup.mp4 -i {}/spedupAudio.wav -c:v copy -c:a aac {}".format(
    TEMP_FOLDER, TEMP_FOLDER, outFile
)
subprocess.call(command, shell=True)

print("Finished.")
timeLength = round(time.time() - startTime, 2)
minutes = timedelta(seconds=(round(timeLength)))
print(f"took {timeLength} seconds ({minutes})")

rmtree(TEMP_FOLDER)
