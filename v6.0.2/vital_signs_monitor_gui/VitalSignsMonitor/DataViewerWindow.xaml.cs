// ============================================================
// DataViewerWindow.xaml.cs -- MATLAB-style figure window
//   selectable signal plot + raw data table + save / export
// ============================================================

using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Win32;
using ScottPlot;
using VitalSignsMonitor.Resources;

namespace VitalSignsMonitor;

public partial class DataViewerWindow : Window
{
    // White-theme palette shared with the main window.
    private static readonly ScottPlot.Color BgColor = ScottPlot.Color.FromHex("#FFFFFF");
    private static readonly ScottPlot.Color FgColor = ScottPlot.Color.FromHex("#6B7280");
    private static readonly ScottPlot.Color GridColor = ScottPlot.Color.FromHex("#E5E7EB");
    private static readonly ScottPlot.Color HrColor = ScottPlot.Color.FromHex("#EC4899");
    private static readonly ScottPlot.Color RespColor = ScottPlot.Color.FromHex("#0EA5E9");
    private static readonly ScottPlot.Color RssiColor = ScottPlot.Color.FromHex("#F59E0B");
    private static readonly ScottPlot.Color TimeColor = ScottPlot.Color.FromHex("#3B82F6");
    private static readonly ScottPlot.Color FftColor = ScottPlot.Color.FromHex("#8B5CF6");

    private readonly IReadOnlyList<VitalsData> _records;
    private readonly VitalsData? _snapshot;
    private static ResourceExtension T => ResourceExtension.Instance;

    public DataViewerWindow(IReadOnlyList<VitalsData> records, VitalsData? latestSnapshot)
    {
        InitializeComponent();
        _records = records;
        _snapshot = latestSnapshot;

        ConfigurePlotStyle();

        var rows = new List<VitalsRow>(_records.Count);
        for (int i = 0; i < _records.Count; i++)
        {
            var d = _records[i];
            rows.Add(new VitalsRow
            {
                Index = i,
                Timestamp = d.Ts,
                HR = d.Hr,
                RR = d.Rr,
                RSSI = d.Rssi
            });
        }
        DataGrid.ItemsSource = rows;

        CountText.Text = string.Format(T["PointCount"], _records.Count);

        SignalSelector.SelectedIndex = 0;
    }

    private void ConfigurePlotStyle()
    {
        var style = MainPlot.Plot.GetStyle();
        style.FigureBackgroundColor = BgColor;
        style.DataBackgroundColor = BgColor;
        style.AxisColor = FgColor;
        style.GridMajorLineColor = GridColor;
        MainPlot.Plot.SetStyle(style);
    }

    // ---- signal selection ----
    private void SignalSelector_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        // Fire only after the combo has been rendered (avoid the initial NRE on load).
        if (!IsLoaded) return;
        Redraw();
    }

    private void ShowPoints_Changed(object sender, RoutedEventArgs e)
    {
        if (!IsLoaded) return;
        Redraw();
    }

    private void Redraw()
    {
        int sel = SignalSelector.SelectedIndex;
        var plt = MainPlot.Plot;
        plt.Clear();
        bool showPoints = ShowPointsCheck.IsChecked == true;

        switch (sel)
        {
            case 0:
                DrawTrend(plt, GetValues(d => d.Hr), HrColor, "Heart Rate", "Sample #", "Heart Rate (bpm)", showPoints);
                break;
            case 1:
                DrawTrend(plt, GetValues(d => d.Rr), RespColor, "Respiration Rate", "Sample #", "Respiration Rate (rpm)", showPoints);
                break;
            case 2:
                DrawArray(plt, _snapshot?.TimeAxis, _snapshot?.TimeWave, TimeColor, "Time Domain", "Time (s)", "Amplitude", showPoints);
                break;
            case 3:
                DrawArray(plt, _snapshot?.FftFreq, _snapshot?.FftMag, FftColor, "FFT Spectrum", "Frequency (Hz)", "Magnitude", showPoints);
                break;
            case 4:
                DrawTrend(plt, GetValues(d => d.Rssi), RssiColor, "RSSI", "Sample #", "RSSI (dBm)", showPoints);
                break;
        }

        MainPlot.Refresh();
    }

    private double[] GetValues(System.Func<VitalsData, double> selector)
    {
        var arr = new double[_records.Count];
        for (int i = 0; i < _records.Count; i++) arr[i] = selector(_records[i]);
        return arr;
    }

    private static void DrawTrend(Plot plt, double[] ys, ScottPlot.Color color,
                                  string title, string xLabel, string yLabel, bool showPoints)
    {
        if (ys.Length == 0) return;

        double[] xs = new double[ys.Length];
        for (int i = 0; i < ys.Length; i++) xs[i] = i;

        var scatter = plt.Add.Scatter(xs, ys);
        scatter.Color = color;
        scatter.LineWidth = 2;
        scatter.MarkerSize = showPoints ? 6 : 0;

        plt.Title(title);
        plt.XLabel(xLabel);
        plt.YLabel(yLabel);
        plt.Axes.AutoScale();
    }

    private static void DrawArray(Plot plt, double[]? xs, double[]? ys, ScottPlot.Color color,
                                  string title, string xLabel, string yLabel, bool showPoints)
    {
        if (ys is not { Length: > 1 })
        {
            plt.Title(title + " - " + T["DataViewerEmpty"]);
            return;
        }

        // Build (x, y) arrays; fall back to index-based X when no axis is provided.
        double[] ya = ys!;
        double[] xa = (xs != null && xs.Length == ya.Length)
            ? xs
            : Enumerable.Range(0, ya.Length).Select(i => (double)i).ToArray();

        var scatter = plt.Add.Scatter(xa, ya);
        scatter.Color = color;
        scatter.LineWidth = 1.5f;
        scatter.MarkerSize = showPoints ? 4 : 0;

        plt.Title(title);
        plt.XLabel(xLabel);
        plt.YLabel(yLabel);
        plt.Axes.AutoScale();
    }

    // ---- actions ----
    private void SaveFigure_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new SaveFileDialog
        {
            Title = T["SaveFigure"],
            Filter = "PNG image (*.png)|*.png|JPEG image (*.jpg)|*.jpg|SVG vector (*.svg)|*.svg",
            InitialDirectory = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
            FileName = "figure.png"
        };

        if (dlg.ShowDialog() != true) return;

        string ext = Path.GetExtension(dlg.FileName).ToLowerInvariant();
        int w = 1200, h = 700;
        try
        {
            switch (ext)
            {
                case ".jpg":
                    MainPlot.Plot.SaveJpeg(dlg.FileName, w, h);
                    break;
                case ".svg":
                    MainPlot.Plot.SaveSvg(dlg.FileName, w, h);
                    break;
                default:
                    MainPlot.Plot.SavePng(dlg.FileName, w, h);
                    break;
            }
            MessageBox.Show(string.Format(T["FigureSaved"], dlg.FileName),
                            T["SaveFigure"], MessageBoxButton.OK, MessageBoxImage.Information);
        }
        catch (System.Exception ex)
        {
            MessageBox.Show(ex.Message, T["SaveFigure"], MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    private void ExportData_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new SaveFileDialog
        {
            Title = T["ExportData"],
            Filter = "CSV file (*.csv)|*.csv",
            InitialDirectory = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
            FileName = "vitals_data.csv"
        };

        if (dlg.ShowDialog() != true) return;

        try
        {
            using var sw = new StreamWriter(dlg.FileName);
            sw.WriteLine("index,timestamp,heart_rate,resp_rate,rssi_dbm");
            for (int i = 0; i < _records.Count; i++)
            {
                var d = _records[i];
                sw.WriteLine($"{i},{d.Ts},{d.Hr:F1},{d.Rr:F1},{d.Rssi}");
            }
            MessageBox.Show(string.Format(T["DataExported"], dlg.FileName),
                            T["ExportData"], MessageBoxButton.OK, MessageBoxImage.Information);
        }
        catch (System.Exception ex)
        {
            MessageBox.Show(ex.Message, T["ExportData"], MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    private void Close_Click(object sender, RoutedEventArgs e) => Close();
}


// Row model for the data grid.
public class VitalsRow
{
    public int Index { get; set; }
    public string Timestamp { get; set; } = "";
    public double HR { get; set; }
    public double RR { get; set; }
    public int RSSI { get; set; }
}
