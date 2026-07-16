// ============================================================
// MainWindow.xaml.cs -- white-theme + i18n + WiFi config via MQTT
// ============================================================

using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using MQTTnet;
using MQTTnet.Client;
using ScottPlot;
using VitalSignsMonitor.Resources;

namespace VitalSignsMonitor;

public partial class MainWindow : Window
{
    private const string WifiConfigTopic = "me41004/config";

    private IMqttClient? _mqtt;
    private MqttClientOptions? _options;
    private int _messageCount;
    private bool _isConnected;

    private readonly List<double> _hrHistory = new();
    private readonly List<double> _respHistory = new();
    private const int MaxTrendPoints = 60;

    // ---- ScottPlot colors (white theme) ----
    private static readonly ScottPlot.Color BgColor   = ScottPlot.Color.FromHex("#FFFFFF");
    private static readonly ScottPlot.Color FgColor   = ScottPlot.Color.FromHex("#6B7280");
    private static readonly ScottPlot.Color GridColor = ScottPlot.Color.FromHex("#E5E7EB");
    private static readonly ScottPlot.Color RespColor = ScottPlot.Color.FromHex("#0EA5E9");
    private static readonly ScottPlot.Color HrColor   = ScottPlot.Color.FromHex("#EC4899");
    private static readonly ScottPlot.Color TimeColor = ScottPlot.Color.FromHex("#3B82F6");
    private static readonly ScottPlot.Color FftColor  = ScottPlot.Color.FromHex("#8B5CF6");

    private double _hrUpper = 120;
    private double _rrUpper = 30;

    private static ResourceExtension T => ResourceExtension.Instance;


    public MainWindow()
    {
        InitializeComponent();
        InitPlots();
    }


    // ============================================================
    // Chart initialization (white theme)
    // ============================================================
    private void InitPlots()
    {
        ConfigurePlotStyle(TimeDomainPlot.Plot,  T["TimeDomain"],  "Time (s)",       "Amplitude");
        ConfigurePlotStyle(FftPlot.Plot,         T["FftSpectrum"], "Frequency (Hz)", "Magnitude");
        ConfigurePlotStyle(RespTrendPlot.Plot,   T["RespRate"],    "Measurement",    "Rate (rpm)");
        ConfigurePlotStyle(HrTrendPlot.Plot,     T["HrRate"],      "Measurement",    "Rate (bpm)");

        TimeDomainPlot.Refresh();
        FftPlot.Refresh();
        RespTrendPlot.Refresh();
        HrTrendPlot.Refresh();
    }

    private void ConfigurePlotStyle(Plot plt, string title, string xLabel, string yLabel)
    {
        plt.Title(title);
        plt.XLabel(xLabel);
        plt.YLabel(yLabel);

        var style = plt.GetStyle();
        style.FigureBackgroundColor = BgColor;
        style.DataBackgroundColor   = BgColor;
        style.AxisColor             = FgColor;
        style.GridMajorLineColor    = GridColor;
        plt.SetStyle(style);
    }


    // ============================================================
    // Language switching
    // ============================================================
    private void CN_Click(object sender, RoutedEventArgs e)
    {
        T.CurrentCulture = "zh-CN";
        RefreshPlotLabels();
    }

    private void EN_Click(object sender, RoutedEventArgs e)
    {
        T.CurrentCulture = "en";
        RefreshPlotLabels();
    }

    private void RefreshPlotLabels()
    {
        TimeDomainPlot.Plot.Title(T["TimeDomain"]);
        TimeDomainPlot.Plot.XLabel("Time (s)");
        TimeDomainPlot.Plot.YLabel("Amplitude");

        FftPlot.Plot.Title(T["FftSpectrum"]);
        FftPlot.Plot.XLabel("Frequency (Hz)");
        FftPlot.Plot.YLabel("Magnitude");

        RespTrendPlot.Plot.Title(T["RespRate"]);
        RespTrendPlot.Plot.XLabel("Measurement");
        RespTrendPlot.Plot.YLabel("Rate (rpm)");

        HrTrendPlot.Plot.Title(T["HrRate"]);
        HrTrendPlot.Plot.XLabel("Measurement");
        HrTrendPlot.Plot.YLabel("Rate (bpm)");

        TimeDomainPlot.Refresh();
        FftPlot.Refresh();
        RespTrendPlot.Refresh();
        HrTrendPlot.Refresh();

        UpdateConnectButton();

        if (!_isConnected)
            StatusText.Text = T["StatusNotConnected"];
    }


    // ============================================================
    // WiFi config -- publish to ESP32 over MQTT
    // ============================================================
    private async void WifiSet_Click(object sender, RoutedEventArgs e)
    {
        Button_Disable(ApplyWifiBtn, 3000);

        if (!_isConnected || _mqtt == null || !_mqtt.IsConnected)
        {
            StatusText.Text = T["WifiNotConnected"];
            return;
        }

        string ssid = WifiSsidInput.Text.Trim();
        string pwd  = WifiPwdInput.Password;

        if (string.IsNullOrEmpty(ssid))
        {
            StatusText.Text = T["WifiName"] + " ?";
            return;
        }

        string json = JsonSerializer.Serialize(new { ssid, password = pwd });
        await _mqtt.PublishStringAsync(WifiConfigTopic, json);
        StatusText.Text = T["WifiSent"];
    }


    // ============================================================
    // Connect / Disconnect MQTT
    // ============================================================
    private async void ConnectBtn_Click(object sender, RoutedEventArgs e)
    {
        if (_isConnected)
        {
            if (_mqtt != null && _mqtt.IsConnected)
                await _mqtt.DisconnectAsync();
            _isConnected = false;
            UpdateConnectButton();
        }
        else
        {
            Button_Disable(ConnectBtn, 3000);
            await ConnectMqtt();
        }
    }

    private async Task ConnectMqtt()
    {
        string broker = BrokerInput.Text.Trim();
        if (!int.TryParse(PortInput.Text.Trim(), out int port))
            port = 63992;
        string topic = TopicInput.Text.Trim();
        if (string.IsNullOrEmpty(topic))
            topic = "me41004/vitals";

        var factory = new MqttFactory();
        _mqtt = factory.CreateMqttClient();

        _options = new MqttClientOptionsBuilder()
            .WithTcpServer(broker, port)
            .WithClientId($"vitals-gui-{Guid.NewGuid():N}".Substring(0, 20))
            .Build();

        _mqtt.ApplicationMessageReceivedAsync += OnMqttMessage;

        _mqtt.DisconnectedAsync += async _ =>
        {
            _isConnected = false;
            Dispatcher.Invoke(UpdateConnectButton);
            await Task.Delay(3000);
            try { if (_options != null) await _mqtt!.ConnectAsync(_options); }
            catch { }
        };

        try
        {
            await _mqtt.ConnectAsync(_options);
            await _mqtt.SubscribeAsync(new MqttTopicFilterBuilder()
                .WithTopic(topic)
                .WithAtMostOnceQoS()
                .Build());

            _isConnected = true;
            UpdateConnectButton();
            StatusText.Text = string.Format(T["StatusConnected"], broker, port);
        }
        catch (Exception ex)
        {
            StatusText.Text = string.Format(T["StatusConnectFail"], ex.Message);
        }
    }

    private void UpdateConnectButton()
    {
        if (_isConnected)
        {
            ConnectBtn.Content = T["Disconnect"];
            ConnectBtn.Background = new SolidColorBrush(System.Windows.Media.Color.FromRgb(34, 197, 94));
            ConnectBtn.Foreground = Brushes.White;
            StatusDot.Fill = new SolidColorBrush(System.Windows.Media.Color.FromRgb(14, 165, 233));
        }
        else
        {
            ConnectBtn.Content = T["ConnectMqtt"];
            ConnectBtn.Background = new SolidColorBrush(System.Windows.Media.Color.FromRgb(107, 114, 128));
            ConnectBtn.Foreground = Brushes.White;
            StatusDot.Fill = new SolidColorBrush(System.Windows.Media.Color.FromRgb(239, 68, 68));
        }
    }


    // ============================================================
    // MQTT message handling + chart updates
    // ============================================================
    private Task OnMqttMessage(MqttApplicationMessageReceivedEventArgs e)
    {
        _messageCount++;
        string json = Encoding.UTF8.GetString(e.ApplicationMessage.PayloadSegment);

        try
        {
            var data = JsonSerializer.Deserialize<VitalsData>(json);
            if (data == null) return Task.CompletedTask;
            Dispatcher.Invoke(() => UpdatePlots(data));
        }
        catch (JsonException) { }

        return Task.CompletedTask;
    }

    private void UpdatePlots(VitalsData data)
    {
        RespValue.Text = data.Rr.ToString("F0");
        HrValue.Text = data.Hr.ToString("F0");

        FooterText.Text =
            $"RSSI: {data.Rssi} dBm  |  HR: {data.Hr:F1} bpm  |  RR: {data.Rr:F1} rpm  |  " +
            $"Msg #{_messageCount}  |  {data.Ts}";

        if (data.TimeAxis is { Length: > 1 } && data.TimeWave is { Length: > 1 })
        {
            var plt = TimeDomainPlot.Plot;
            plt.Clear();
            var sig = plt.Add.SignalXY(data.TimeAxis, data.TimeWave);
            sig.Color = TimeColor;
            sig.LineWidth = 1.5f;
            plt.Axes.AutoScale();
            TimeDomainPlot.Refresh();
        }

        if (data.FftFreq is { Length: > 1 } && data.FftMag is { Length: > 1 })
        {
            var plt = FftPlot.Plot;
            plt.Clear();
            var sig = plt.Add.SignalXY(data.FftFreq, data.FftMag);
            sig.Color = FftColor;
            sig.LineWidth = 1.5f;
            plt.Add.VerticalLine(0.3, color: RespColor.WithAlpha(0.4));
            plt.Add.VerticalLine(1.2, color: HrColor.WithAlpha(0.4));
            plt.Axes.AutoScale();
            FftPlot.Refresh();
        }

        _respHistory.Add(data.Rr);
        if (_respHistory.Count > MaxTrendPoints) _respHistory.RemoveAt(0);

        {
            var plt = RespTrendPlot.Plot;
            plt.Clear();
            if (_respHistory.Count > 1)
            {
                double[] xs = Enumerable.Range(0, _respHistory.Count)
                    .Select(i => (double)i).ToArray();
                var sig = plt.Add.SignalXY(xs, _respHistory.ToArray());
                sig.Color = RespColor;
                sig.LineWidth = 2;
                plt.Axes.SetLimitsY(0, _rrUpper);
            }
            RespTrendPlot.Refresh();
        }

        _hrHistory.Add(data.Hr);
        if (_hrHistory.Count > MaxTrendPoints) _hrHistory.RemoveAt(0);

        {
            var plt = HrTrendPlot.Plot;
            plt.Clear();
            if (_hrHistory.Count > 1)
            {
                double[] xs = Enumerable.Range(0, _hrHistory.Count)
                    .Select(i => (double)i).ToArray();
                var sig = plt.Add.SignalXY(xs, _hrHistory.ToArray());
                sig.Color = HrColor;
                sig.LineWidth = 2;
                plt.Axes.SetLimitsY(40, _hrUpper);
            }
            HrTrendPlot.Refresh();
        }
    }


    // ============================================================
    // Button handlers
    // ============================================================
    private async void ApplyMqtt_Click(object sender, RoutedEventArgs e)
    {
        Button_Disable(ApplyMqttBtn, 3000);

        if (_isConnected && _mqtt != null && _mqtt.IsConnected)
        {
            await _mqtt.DisconnectAsync();
            _isConnected = false;
            UpdateConnectButton();
        }

        StatusText.Text = T["StatusMqttUpdated"];
    }

    private void ClearData_Click(object sender, RoutedEventArgs e)
    {
        _hrHistory.Clear();
        _respHistory.Clear();
        RespTrendPlot.Plot.Clear();
        HrTrendPlot.Plot.Clear();
        RespTrendPlot.Refresh();
        HrTrendPlot.Refresh();
        StatusText.Text = T["StatusTrendCleared"];
    }

    private void ExportScreenshot_Click(object sender, RoutedEventArgs e)
    {
        Button_Disable((Button)sender, 5000);
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        TimeDomainPlot.Plot.SavePng(Path.Combine(dir, "time_domain.png"), 600, 400);
        FftPlot.Plot.SavePng(Path.Combine(dir, "fft_spectrum.png"), 600, 400);
        RespTrendPlot.Plot.SavePng(Path.Combine(dir, "resp_trend.png"), 600, 400);
        HrTrendPlot.Plot.SavePng(Path.Combine(dir, "hr_trend.png"), 600, 400);
        StatusText.Text = string.Format(T["ScreenshotSaved"], dir);
    }

    private void ExportLog_Click(object sender, RoutedEventArgs e)
    {
        Button_Disable((Button)sender, 5000);
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string path = Path.Combine(dir, $"vitals_log_{DateTime.Now:yyyyMMdd_HHmmss}.csv");

        using var sw = new StreamWriter(path);
        sw.WriteLine("index,heart_rate,resp_rate");
        int max = Math.Max(_hrHistory.Count, _respHistory.Count);
        for (int i = 0; i < max; i++)
        {
            double hr = i < _hrHistory.Count ? _hrHistory[i] : 0;
            double rr = i < _respHistory.Count ? _respHistory[i] : 0;
            sw.WriteLine($"{i},{hr:F1},{rr:F1}");
        }

        StatusText.Text = string.Format(T["LogSaved"], path);
    }


    // ============================================================
    // Button anti-spam
    // ============================================================
    private async void Button_Disable(Button target, int timeMs)
    {
        target.IsEnabled = false;
        await Task.Delay(timeMs);
        target.IsEnabled = true;
    }


    // ============================================================
    // Cleanup
    // ============================================================
    protected override void OnClosed(EventArgs e)
    {
        if (_mqtt != null)
        {
            if (_mqtt.IsConnected) _mqtt.DisconnectAsync().Wait(2000);
            _mqtt.Dispose();
        }
        base.OnClosed(e);
    }
}


// ---- JSON data model ----
public class VitalsData
{
    [JsonPropertyName("ts")]        public string Ts { get; set; } = "";
    [JsonPropertyName("hr")]        public double Hr { get; set; }
    [JsonPropertyName("rr")]        public double Rr { get; set; }
    [JsonPropertyName("rssi")]      public int Rssi { get; set; }
    [JsonPropertyName("time_axis")] public double[]? TimeAxis { get; set; }
    [JsonPropertyName("time_wave")] public double[]? TimeWave { get; set; }
    [JsonPropertyName("fft_freq")]  public double[]? FftFreq { get; set; }
    [JsonPropertyName("fft_mag")]   public double[]? FftMag { get; set; }
}
