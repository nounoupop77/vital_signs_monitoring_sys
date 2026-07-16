# Vital Signs Monitor -- Python Analysis + C# WPF GUI Tutorial

## How the system works

    ESP32 (WiFi CSI)
         |
         | publishes raw CSI to MQTT topic: me41004/csi
         v
    +--------------------+
    | csi_subscriber.py  |   (Python -- the brain)
    | - reads CSI data   |
    | - bandpass filter  |
    | - FFT -> HR / RR   |
    | - ALSO publishes   |------> MQTT topic: me41004/vitals
    |   results as JSON  |           (JSON with HR, RR, waveform, FFT)
    +--------------------+                  |
                                            v
                                   +------------------+
                                   | VitalSignsMonitor |  (C# WPF -- the display)
                                   | - subscribes     |
                                   | - parses JSON    |
                                   | - draws 4 charts |
                                   +------------------+

The KEY idea: Python does all the math (filtering, FFT), then sends
the RESULTS to C# via MQTT. C# only draws pictures. This keeps each
side simple.

## What changed in your Python script

Your original csi_subscriber.py only printed to console and wrote CSV.
The modified version (in the python/ folder) adds 3 things:

1. compute_rate_full() -- same as compute_rate() but also returns
   the filtered waveform and FFT spectrum arrays (for plotting).

2. publish_vitals() -- packages HR, RR, waveform, FFT into a JSON
   message and sends it to MQTT topic me41004/vitals.

3. In on_message(), after computing HR/RR, it calls publish_vitals()
   so the C# GUI gets fresh data.

All your original code (CSV writing, console output, filtering logic)
is preserved and unchanged.

## Step-by-step setup

### Step 1: Install Python dependencies

    py -m pip install paho-mqtt numpy scipy

### Step 2: Run the Python script

    cd python
    py csi_subscriber.py

You should see:

    [10:30:00] CONNECTED, subscribing to 'me41004/csi'
    [10:30:12] Heart Rate: 72.0 bpm   Resp: 16.0 br/min   (buf=400)
    [10:30:12] -> published to me41004/vitals: HR=72.0 RR=16.0

### Step 3: Install .NET 8 SDK

Download from https://dotnet.microsoft.com/download
(you already have .NET 10 SDK, which also works)

### Step 4: Build and run the C# GUI

    cd VitalSignsMonitor
    dotnet restore
    dotnet run

A dark-themed window opens with 4 charts that update in real time.

## How C# receives data (the important part)

Here is the complete flow, explained step by step:

### 1. Connect to MQTT

In MainWindow.xaml.cs, the ConnectMqtt() method creates an MQTT client
and subscribes to me41004/vitals:

    var factory = new MqttFactory();
    _mqtt = factory.CreateMqttClient();

    var options = new MqttClientOptionsBuilder()
        .WithTcpServer("xg-6.frp.one", 63992)
        .Build();

    await _mqtt.ConnectAsync(options);
    await _mqtt.SubscribeAsync(
        new MqttTopicFilterBuilder()
            .WithTopic("me41004/vitals")
            .Build());

### 2. Receive messages

When Python publishes a message, MQTTnet calls OnMqttMessage:

    _mqtt.ApplicationMessageReceivedAsync += OnMqttMessage;

Inside that method, we decode the bytes to a string, then parse JSON:

    string json = Encoding.UTF8.GetString(e.ApplicationMessage.PayloadSegment);
    var data = JsonSerializer.Deserialize<VitalsData>(json);

### 3. Update the UI

MQTT callbacks run on a background thread, but WPF controls can only
be touched from the UI thread. We bridge this with Dispatcher.Invoke:

    Dispatcher.Invoke(() => UpdatePlots(data));

### 4. Draw charts

Each chart is a ScottPlot WpfPlot control. To update:

    var plt = TimeDomainPlot.Plot;
    plt.Clear();
    var sig = plt.Add.SignalXY(data.TimeAxis, data.TimeWave);
    sig.Color = TimeColor;
    TimeDomainPlot.Refresh();

## JSON message format (me41004/vitals)

Python sends this JSON. The C# VitalsData class maps each field:

    {
      "ts": "2026-07-15T10:30:00",
      "hr": 72.5,
      "rr": 16.0,
      "rssi": -54,
      "time_axis": [0.0, 0.025, 0.05, ...],
      "time_wave": [0.12, 0.15, 0.08, ...],
      "fft_freq":  [0.0, 0.02, 0.04, ...],
      "fft_mag":   [0.5, 0.3, 0.1, ...]
    }

## Project structure

    vital_signs_monitor/
    |
    +-- python/
    |   +-- csi_subscriber.py     (modified -- adds MQTT publishing)
    |   +-- requirements.txt
    |
    +-- VitalSignsMonitor/
    |   +-- VitalSignsMonitor.csproj
    |   +-- App.xaml / App.xaml.cs
    |   +-- MainWindow.xaml        (UI layout: 4 charts + status bar)
    |   +-- MainWindow.xaml.cs     (MQTT subscriber + chart updates)
    |   +-- app.manifest
    |
    +-- README.md                  (this file)

## Changing the MQTT broker address

If you use a different broker, change it in TWO places:

1. Python: csi_subscriber.py -> --broker argument
2. C#: MainWindow.xaml.cs -> ConnectMqtt() method

## Troubleshooting

- "No data" in the GUI: make sure the Python script is running first.
  C# only receives data that Python publishes.
- "Connection failed": check that the broker IP/port is reachable.
- Charts not updating: check that Python prints "-> published to
  me41004/vitals" in its console output.
- Build error about NuGet.Config: run "dotnet restore" outside the
  sandbox / with administrator rights if needed.
