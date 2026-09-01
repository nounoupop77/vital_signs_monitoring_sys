using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.CompilerServices;

namespace VitalSignsMonitor.Resources;

/// <summary>
/// Lightweight i18n singleton.
/// Bind in XAML:  {Binding Source={x:Static res:ResourceExtension.Instance}, Path=SomeKey}
/// Switch culture in code:  ResourceExtension.Instance.CurrentCulture = "en";
/// </summary>
public sealed class ResourceExtension : INotifyPropertyChanged
{
    private static readonly ResourceExtension _instance = new();
    public static ResourceExtension Instance => _instance;

    private string _currentCulture = "zh-CN";

    // ---- resource tables ----
    private static readonly Dictionary<string, string> Zh = new()
    {
        ["AppTitle"]           = "Vital Sign Monitor System",

        ["StatusWaiting"]      = "\u7b49\u5f85\u6570\u636e...",
        ["StatusConnected"]    = "\u5df2\u8fde\u63a5 - \u7b49\u5f85\u6570\u636e ({0}:{1})",
        ["StatusConnectFail"]  = "\u8fde\u63a5\u5931\u8d25: {0}",
        ["StatusNotConnected"] = "\u672a\u8fde\u63a5",
        ["StatusMqttUpdated"]  = "MQTT \u53c2\u6570\u5df2\u66f4\u65b0\uff0c\u8bf7\u70b9\u51fb[\u8fde\u63a5]",
        ["StatusTrendCleared"] = "\u8d8b\u52bf\u6570\u636e\u5df2\u6e05\u9664",
        ["ScreenshotSaved"]    = "\u622a\u56fe\u5df2\u4fdd\u5b58\u5230 {0}",
        ["LogSaved"]           = "\u65e5\u5fd7\u5df2\u4fdd\u5b58\u5230 {0}",
        ["FigureSaved"]        = "\u56fe\u8868\u5df2\u4fdd\u5b58\u5230 {0}",
        ["DataExported"]       = "\u6570\u636e\u5df2\u5bfc\u51fa\u5230 {0}",
        ["NoData"]             = "\u6682\u65e0\u6570\u636e\uff0c\u8bf7\u5148\u8fde\u63a5\u5e76\u91c7\u96c6",
        ["SaveFolderMissing"]  = "\u4fdd\u5b58\u8def\u5f84\u4e3a\u7a7a\u6216\u4e0d\u5b58\u5728\uff0c\u5df2\u4f7f\u7528\u9ed8\u8ba4\u4f4d\u7f6e",
        ["FolderCreated"]      = "\u5df2\u81ea\u52a8\u521b\u5efa\u4fdd\u5b58\u6587\u4ef6\u5939",
        ["DataViewerEmpty"]    = "\u65e0\u6570\u636e\u53ef\u67e5\u770b",

        ["TimeDomain"]         = "Time Domain",
        ["FftSpectrum"]        = "FFT Spectrum",
        ["RespRate"]           = "Respiration Rate",
        ["HrRate"]             = "Heart Rate",
        ["RespLabel"]          = "RESPIRATION",
        ["HrLabel"]            = "HEART RATE",

        ["WifiTab"]            = "WiFi \u72b6\u6001",
        ["WifiSsid"]           = "\u5f53\u524d WiFi",
        ["WifiIp"]             = "IP \u5730\u5740",
        ["WifiRssi"]           = "\u4fe1\u53f7\u5f3a\u5ea6",
        ["WifiHint"]           = "\u663e\u793a ESP32 \u5f53\u524d\u8fde\u63a5\u7684 WiFi\uff0c\u8fde\u63a5\u540e\u81ea\u52a8\u66f4\u65b0\uff1b\u5982\u9700\u66f4\u6539 WiFi\uff0c\u8bf7\u5728\u5f00\u673a\u65f6\u6309\u4f4f BOOT \u952e\u8fdb\u5165\u914d\u7f51\u9875",

        ["MqttTab"]            = "MQTT \u8fde\u63a5",
        ["DisplayTab"]         = "\u663e\u793a\u8bbe\u7f6e",
        ["DataTab"]            = "\u6570\u636e\u64cd\u4f5c",
        ["Broker"]             = "\u670d\u52a1\u5668",
        ["Port"]               = "\u7aef\u53e3",
        ["Topic"]              = "\u4e3b\u9898",
        ["ApplyMqtt"]          = "\u5e94\u7528 MQTT \u8bbe\u7f6e",
        ["HrUpper"]            = "\u5fc3\u7387\u4e0a\u9650",
        ["RrUpper"]            = "\u547c\u5438\u4e0a\u9650",
        ["ClearTrend"]         = "\u6e05\u9664\u8d8b\u52bf\u6570\u636e",
        ["ExportScreenshot"]   = "\u5bfc\u51fa\u8d8b\u52bf\u622a\u56fe",
        ["SaveDataLog"]        = "\u4fdd\u5b58\u6570\u636e\u65e5\u5fd7",
        ["SavePath"]           = "\u4fdd\u5b58\u8def\u5f84",
        ["Browse"]             = "\u6d4f\u89c8...",
        ["OpenDataViewer"]     = "\u6570\u636e\u67e5\u770b\u5668",
        ["ViewerTitle"]        = "\u6570\u636e\u67e5\u770b\u5668 - Data Viewer",
        ["SelectSignal"]       = "\u9009\u62e9\u4fe1\u53f7",
        ["HeartRateSignal"]    = "\u5fc3\u7387\u8d8b\u52bf",
        ["RespRateSignal"]     = "\u547c\u5438\u7387\u8d8b\u52bf",
        ["RssiSignal"]         = "\u4fe1\u53f7\u5f3a\u5ea6 RSSI",
        ["WaveformSignal"]     = "\u65f6\u57df\u6ce2\u5f62",
        ["FftSignal"]          = "\u9891\u8c31 FFT",
        ["ShowPoints"]         = "\u663e\u793a\u6570\u636e\u70b9",
        ["SaveFigure"]         = "\u4fdd\u5b58\u56fe\u8868",
        ["ExportData"]         = "\u5bfc\u51fa\u6570\u636e",
        ["CloseBtn"]           = "\u5173\u95ed",
        ["ColIndex"]           = "\u5e8f\u53f7",
        ["ColTimestamp"]       = "\u65f6\u95f4\u6233",
        ["ColHr"]              = "\u5fc3\u7387(bpm)",
        ["ColRr"]              = "\u547c\u5438(rpm)",
        ["ColRssi"]            = "RSSI(dBm)",
        ["PointCount"]         = "\u5171 {0} \u4e2a\u6570\u636e\u70b9",
        ["ConnectMqtt"]        = "\u8fde\u63a5 MQTT",
        ["Disconnect"]         = "\u65ad\u5f00\u8fde\u63a5",
        ["Language"]           = "\u8bed\u8a00/Language",
        ["Chinese"]            = "\u7b80\u4f53\u4e2d\u6587",
        ["English"]            = "English",
        ["UnitRpm"]            = " rpm",
        ["UnitBpm"]            = " bpm",
    };

    private static readonly Dictionary<string, string> En = new()
    {
        ["AppTitle"]           = "Vital Sign Monitor System",

        ["StatusWaiting"]      = "Waiting for data...",
        ["StatusConnected"]    = "Connected - waiting for data ({0}:{1})",
        ["StatusConnectFail"]  = "Connection failed: {0}",
        ["StatusNotConnected"] = "Not connected",
        ["StatusMqttUpdated"]  = "MQTT settings updated. Click [Connect].",
        ["StatusTrendCleared"] = "Trend data cleared.",
        ["ScreenshotSaved"]    = "Screenshots saved to {0}",
        ["LogSaved"]           = "Log saved to {0}",
        ["FigureSaved"]        = "Figure saved to {0}",
        ["DataExported"]       = "Data exported to {0}",
        ["NoData"]             = "No data yet. Connect and acquire first.",
        ["SaveFolderMissing"]  = "Save path missing or invalid, used default location",
        ["FolderCreated"]      = "Save folder created",
        ["DataViewerEmpty"]    = "No data to view",

        ["TimeDomain"]         = "Time Domain",
        ["FftSpectrum"]        = "FFT Spectrum",
        ["RespRate"]           = "Respiration Rate",
        ["HrRate"]             = "Heart Rate",
        ["RespLabel"]          = "RESPIRATION",
        ["HrLabel"]            = "HEART RATE",

        ["WifiTab"]            = "WiFi Status",
        ["WifiSsid"]           = "Current WiFi",
        ["WifiIp"]             = "IP Address",
        ["WifiRssi"]           = "Signal Strength",
        ["WifiHint"]           = "Shows the WiFi the ESP32 is currently connected to; updates automatically. To change WiFi, hold BOOT during power-up to open the setup portal.",

        ["MqttTab"]            = "MQTT Connection",
        ["DisplayTab"]         = "Display Settings",
        ["DataTab"]            = "Data Operations",
        ["Broker"]             = "Broker",
        ["Port"]               = "Port",
        ["Topic"]              = "Topic",
        ["ApplyMqtt"]          = "Apply MQTT Settings",
        ["HrUpper"]            = "HR Upper Limit",
        ["RrUpper"]            = "RR Upper Limit",
        ["ClearTrend"]         = "Clear Trend Data",
        ["ExportScreenshot"]   = "Export Trend Screenshots",
        ["SaveDataLog"]        = "Save Data Log",
        ["SavePath"]           = "Save Path",
        ["Browse"]             = "Browse...",
        ["OpenDataViewer"]     = "Data Viewer",
        ["ViewerTitle"]        = "Data Viewer",
        ["SelectSignal"]       = "Signal",
        ["HeartRateSignal"]    = "Heart Rate Trend",
        ["RespRateSignal"]     = "Respiration Rate Trend",
        ["RssiSignal"]         = "RSSI Strength",
        ["WaveformSignal"]     = "Time-Domain Waveform",
        ["FftSignal"]          = "FFT Spectrum",
        ["ShowPoints"]         = "Show Points",
        ["SaveFigure"]         = "Save Figure",
        ["ExportData"]         = "Export Data",
        ["CloseBtn"]           = "Close",
        ["ColIndex"]           = "Index",
        ["ColTimestamp"]       = "Timestamp",
        ["ColHr"]              = "HR (bpm)",
        ["ColRr"]              = "RR (rpm)",
        ["ColRssi"]            = "RSSI (dBm)",
        ["PointCount"]         = "{0} data points",
        ["ConnectMqtt"]        = "Connect MQTT",
        ["Disconnect"]         = "Disconnect",
        ["Language"]           = "Language",
        ["Chinese"]            = "Chinese",
        ["English"]            = "English",
        ["UnitRpm"]            = " rpm",
        ["UnitBpm"]            = " bpm",
    };

    private Dictionary<string, string> Active =>
        _currentCulture == "en" ? En : Zh;

    public string CurrentCulture
    {
        get => _currentCulture;
        set
        {
            if (_currentCulture == value) return;
            _currentCulture = value;
            OnPropertyChanged("Item[]");
            OnPropertyChanged("");
        }
    }

    public string this[string key] =>
        Active.TryGetValue(key, out var v) ? v : key;

    // ---- typed accessors for XAML bindings (Path=...) ----
    public string AppTitle           => this["AppTitle"];
    public string StatusWaiting      => this["StatusWaiting"];
    public string StatusNotConnected => this["StatusNotConnected"];
    public string StatusMqttUpdated  => this["StatusMqttUpdated"];
    public string StatusTrendCleared => this["StatusTrendCleared"];
    public string TimeDomain         => this["TimeDomain"];
    public string FftSpectrum        => this["FftSpectrum"];
    public string RespRate           => this["RespRate"];
    public string HrRate             => this["HrRate"];
    public string RespLabel          => this["RespLabel"];
    public string HrLabel            => this["HrLabel"];
    public string WifiTab            => this["WifiTab"];
    public string WifiSsid           => this["WifiSsid"];
    public string WifiIp             => this["WifiIp"];
    public string WifiRssi           => this["WifiRssi"];
    public string WifiHint           => this["WifiHint"];
    public string MqttTab            => this["MqttTab"];
    public string DisplayTab         => this["DisplayTab"];
    public string DataTab            => this["DataTab"];
    public string Broker             => this["Broker"];
    public string Port               => this["Port"];
    public string Topic              => this["Topic"];
    public string ApplyMqtt          => this["ApplyMqtt"];
    public string HrUpper            => this["HrUpper"];
    public string RrUpper            => this["RrUpper"];
    public string ClearTrend         => this["ClearTrend"];
    public string ExportScreenshot   => this["ExportScreenshot"];
    public string SaveDataLog        => this["SaveDataLog"];
    public string SavePath           => this["SavePath"];
    public string Browse             => this["Browse"];
    public string OpenDataViewer     => this["OpenDataViewer"];
    public string ViewerTitle        => this["ViewerTitle"];
    public string SelectSignal       => this["SelectSignal"];
    public string HeartRateSignal    => this["HeartRateSignal"];
    public string RespRateSignal     => this["RespRateSignal"];
    public string RssiSignal         => this["RssiSignal"];
    public string WaveformSignal     => this["WaveformSignal"];
    public string FftSignal          => this["FftSignal"];
    public string ShowPoints         => this["ShowPoints"];
    public string SaveFigure         => this["SaveFigure"];
    public string ExportData         => this["ExportData"];
    public string CloseBtn           => this["CloseBtn"];
    public string ColIndex           => this["ColIndex"];
    public string ColTimestamp       => this["ColTimestamp"];
    public string ColHr              => this["ColHr"];
    public string ColRr              => this["ColRr"];
    public string ColRssi            => this["ColRssi"];
    public string PointCount         => this["PointCount"];
    public string FigureSaved        => this["FigureSaved"];
    public string DataExported       => this["DataExported"];
    public string NoData             => this["NoData"];
    public string SaveFolderMissing  => this["SaveFolderMissing"];
    public string FolderCreated      => this["FolderCreated"];
    public string DataViewerEmpty    => this["DataViewerEmpty"];
    public string ConnectMqtt        => this["ConnectMqtt"];
    public string Disconnect         => this["Disconnect"];
    public string Language           => this["Language"];
    public string Chinese            => this["Chinese"];
    public string English            => this["English"];
    public string UnitRpm            => this["UnitRpm"];
    public string UnitBpm            => this["UnitBpm"];

    public event PropertyChangedEventHandler? PropertyChanged;

    private void OnPropertyChanged([CallerMemberName] string? name = null) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}