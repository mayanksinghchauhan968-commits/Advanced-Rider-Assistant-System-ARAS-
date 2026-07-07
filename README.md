# Smart Helmet with AI Voice Assistant & Intelligent Scooter Authentication

> An IoT-based Smart Helmet system that enhances rider safety using **ESP32**, **ESP-NOW**, **AI Voice Assistant**, **Crash Detection**, **GPS Tracking**, and **Automatic SOS Alerts**.

---

## Overview

This project is a complete smart riding safety system consisting of **two ESP32 boards** and a **Python AI Server**.

The helmet continuously monitors whether the rider is wearing the helmet correctly, detects accidents using an MPU6050 sensor, shares live GPS coordinates during emergencies, and communicates wirelessly with the scooter using ESP-NOW.

The scooter only allows the engine to start when the rider is wearing the helmet with the strap securely fastened.

Additionally, the helmet includes an AI-powered multilingual voice assistant capable of answering questions, opening navigation routes, playing YouTube videos, and providing hands-free assistance while riding.

---

#  Features

###  Smart Helmet

* Helmet detection using IR sensor
* Strap detection
* Engine authorization
* Crash detection using MPU6050
* GPS location tracking
* Automatic SOS through Telegram
* Emergency cancellation button
* ESP-NOW communication
* Heartbeat monitoring
* Helmet authentication

###  Scooter Unit

* Wireless engine authentication
* Relay-controlled ignition lock
* Reverse obstacle detection
* VL53L0X Time-of-Flight distance sensor
* Audible proximity warning
* Automatic engine blocking if communication is lost

###  AI Voice Assistant

* Wake word detection ("AI")
* English
* Hindi
* Gujarati
* Speech-to-Text using Groq Whisper
* Llama AI responses
* Google Text-to-Speech
* Bluetooth headset support
* Automatic YouTube playback
* Google Maps navigation
* Weather information
* Nearest hospital search
* Petrol pump search

---

#  System Architecture

```
                ┌────────────────────┐
                │   Python AI Server │
                │                    │
                │ Groq Whisper       │
                │ Llama AI           │
                │ Google TTS         │
                └─────────┬──────────┘
                          │
                      Wi-Fi TCP
                          │
             ┌────────────┴────────────┐
             │                         │
      Helmet ESP32-S3            Bluetooth Headset
             │
             │ ESP-NOW
             ▼
      Scooter ESP32
             │
     Relay + Buzzer
             │
        Scooter Engine
```

---

#  Hardware Used

## Helmet Unit

* ESP32-S3 Dev Board
* MPU6050
* NEO-6M GPS Module
* IR Sensor
* Strap Switch
* Push Button
* LEDs
* Battery

---

## Scooter Unit

* ESP32 Dev Board
* Relay Module
* VL53L0X ToF Sensor
* Buzzer

---

## AI Server

* Laptop / PC
* Bluetooth Headset
* Python 3.10+

---

#  Working Principle

## 1. Helmet Authentication

When the rider wears the helmet:

✔ IR sensor detects the helmet.

If the strap is locked:

✔ Helmet becomes authenticated.

Helmet sends

```
Engine = ALLOW
```

to the scooter using ESP-NOW.

The scooter energizes the relay and allows engine ignition.

---

## 2. Engine Blocking

If

* helmet removed

OR

* strap opened

OR

* helmet communication lost

the helmet immediately sends

```
Engine = BLOCK
```

The scooter disables ignition.

---

## 3. Crash Detection

The MPU6050 continuously monitors acceleration.

If the acceleration exceeds the crash threshold,

the helmet enters Crash State.

A countdown starts.

The rider can cancel using the emergency button.

Otherwise,

the helmet automatically sends an SOS message with GPS coordinates through Telegram.

---

## 4. Reverse Parking Assist

When the reverse button is pressed,

the VL53L0X measures the obstacle distance.

Depending on distance,

the buzzer frequency increases.

```
>60 cm    Silent

40-60 cm  Slow beep

25-40 cm  Medium beep

15-25 cm  Fast beep

<15 cm    Continuous beep
```

---

## 5. AI Voice Assistant

The Python server continuously listens for the wake word

```
AI
```

Workflow:

Wake Word

↓

Speech Recording

↓

Groq Whisper

↓

Llama AI

↓

Google TTS

↓

Bluetooth Headset

Supported commands include:

* Play music on YouTube
* Navigation
* Weather
* Hospital search
* Petrol pump search
* General AI questions

---

#  Communication

## ESP-NOW

Helmet ESP32

↓

Scooter ESP32

Purpose:

* Engine authorization
* Heartbeat
* Wireless communication

---

## Wi-Fi

Helmet ESP32

↓

Telegram

Purpose:

* SOS alerts
* Helmet authentication messages

---

## TCP Socket

Helmet

↓

Python Server

Purpose:

* Voice Assistant status updates

---

# State Machine

```
STATE_IDLE

↓

Helmet Worn

↓

STATE_HELMET

↓

Strap Locked

↓

STATE_READY

↓

Crash Detected

↓

STATE_CRASH

↓

Countdown

↓

SOS Sent
```

---


#  Arduino Libraries

* WiFi
* ESP-NOW
* TinyGPS++
* MPU6050_tockn
* UniversalTelegramBot
* ArduinoJson
* Adafruit VL53L0X

---

#  Python Libraries

```
pip install

sounddevice
numpy
scipy
groq
gtts
```

Also install

```
FFmpeg
```

and ensure it is added to your system PATH.

---

#  Configuration

Before running the project, update the following:

* Wi-Fi SSID
* Wi-Fi Password
* Telegram Bot Token
* Telegram Chat ID
* Scooter MAC Address
* Groq API Key

---

#  Running the Project

## Step 1

Upload Scooter firmware.

---

## Step 2

Read Scooter MAC address.

---

## Step 3

Update Helmet firmware with Scooter MAC.

---

## Step 4

Upload Helmet firmware.

---

## Step 5

Run Python server

```
python smart_helmet_server.py
```

---

# Applications

* Smart Helmets
* Rider Safety
* Accident Detection
* Emergency Response
* Connected Vehicles
* Intelligent Transportation Systems
* IoT Research
* AI-assisted Mobility

---

#  Future Improvements

* GSM-based emergency calling
* Cloud dashboard
* Mobile application
* Rider health monitoring
* Camera integration
* Blind spot detection
* OTA firmware updates
* Battery monitoring
* Helmet theft detection
* Machine Learning crash classification

---
