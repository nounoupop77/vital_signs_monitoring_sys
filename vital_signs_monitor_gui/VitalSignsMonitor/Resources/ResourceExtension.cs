using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.CompilerServices;

namespace VitalSignsMonitor.Resources;

/// <summary>
/// Lightweight i18n singleton (mirrors XtrayUserInterface's ResourceExtension pattern).
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
        // 固定标题，不随语言切换
        ["AppTitle"]           = "Vital Sign Monitor System 生命体征监测器",

        ["StatusWaiting"]      = "等待数据...",
        ["StatusConnected"]    = "已连接 - 等待数据 ({0}:{1})",
        ["StatusConnectFail"]  = "连接失败: {0}",
        ["StatusNotConnected"] = "未连接",
        ["StatusMqttUpdated"]  = "MQTT 参数已更新，请点击[连接]",
        ["StatusTrendCleared"] = "趋势数据已清除",
        ["ScreenshotSaved"]    = "截图已保存到 {0}",
        ["LogSaved"]           = "日志已保存到 {0}",

        // 科学/医学术语保持英文
        ["TimeDomain"]         = "Time Domain",
        ["FftSpectrum"]        = "FFT Spectrum",
        ["RespRate"]           = "Respiration Rate",
        ["HrRate"]             = "Heart Rate",
        ["RespLabel"]          = "RESPIRATION",
        ["HrLabel"]            = "HEART RATE",

        ["WifiTab"]            = "WiFi 设置",
        ["WifiName"]           = "WiFi 名称",
        ["WifiPwd"]            = "WiFi 密码",
        ["ApplyWifi"]          = "发送 WiFi 配置",
        ["WifiSent"]           = "WiFi 配置已通过 MQTT 发送，ESP32 将重新连接",
        ["WifiNotConnected"]   = "请先连接 MQTT 再发送 WiFi 配置",

        ["MqttTab"]            = "MQTT 连接",
        ["DisplayTab"]         = "显示设置",
        ["DataTab"]            = "数据操作",
        ["Broker"]             = "服务器",
        ["Port"]               = "端口",
        ["Topic"]              = "主题",
        ["ApplyMqtt"]          = "应用 MQTT 设置",
        ["HrUpper"]            = "心率上限",
        ["RrUpper"]            = "呼吸上限",
        ["ClearTrend"]         = "清除趋势数据",
        ["ExportScreenshot"]   = "导出趋势截图",
        ["SaveDataLog"]        = "保存数据日志",
        ["ConnectMqtt"]        = "连接 MQTT",
        ["Disconnect"]         = "断开连接",
        ["Language"]           = "语言/Language",
        ["Chinese"]            = "简体中文",
        ["English"]            = "English",
        ["UnitRpm"]            = " rpm",
        ["UnitBpm"]            = " bpm",
    };

    private static readonly Dictionary<string, string> En = new()
    {
        // 固定标题，不随语言切换
        ["AppTitle"]           = "Vital Sign Monitor System 生命体征监测器",

        ["StatusWaiting"]      = "Waiting for data...",
        ["StatusConnected"]    = "Connected - waiting for data ({0}:{1})",
        ["StatusConnectFail"]  = "Connection failed: {0}",
        ["StatusNotConnected"] = "Not connected",
        ["StatusMqttUpdated"]  = "MQTT settings updated. Click [Connect].",
        ["StatusTrendCleared"] = "Trend data cleared.",
        ["ScreenshotSaved"]    = "Screenshots saved to {0}",
        ["LogSaved"]           = "Log saved to {0}",

        ["TimeDomain"]         = "Time Domain",
        ["FftSpectrum"]        = "FFT Spectrum",
        ["RespRate"]           = "Respiration Rate",
        ["HrRate"]             = "Heart Rate",
        ["RespLabel"]          = "RESPIRATION",
        ["HrLabel"]            = "HEART RATE",

        ["WifiTab"]            = "WiFi Settings",
        ["WifiName"]           = "WiFi SSID",
        ["WifiPwd"]            = "WiFi Password",
        ["ApplyWifi"]          = "Send WiFi Config",
        ["WifiSent"]           = "WiFi config sent via MQTT, ESP32 will reconnect",
        ["WifiNotConnected"]   = "Connect to MQTT first before sending WiFi config",

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
        ["ConnectMqtt"]        = "Connect MQTT",
        ["Disconnect"]         = "Disconnect",
        ["Language"]           = "Language",
        ["Chinese"]            = "简体中文",
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
    public string WifiName           => this["WifiName"];
    public string WifiPwd            => this["WifiPwd"];
    public string ApplyWifi          => this["ApplyWifi"];
    public string WifiSent           => this["WifiSent"];
    public string WifiNotConnected   => this["WifiNotConnected"];
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
