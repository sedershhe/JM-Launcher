using System.Globalization;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Infrastructure;
using Microsoft.Win32;

namespace JML.Pages;

/// <summary>
/// 设置页的「插件设置」动态表单（任务 3-B）。
///
/// 数据来源：Python <c>list_plugin_settings()</c>（manifest config.schema 枚举）；
/// 渲染规则（契约《插件设置项》一节）：
/// <list type="bullet">
/// <item>无插件设置 / 读取失败 → 空态文字「当前没有插件提供设置项」（失败另加一行红字）；</item>
/// <item>text / path → TextBox（path 追加「浏览…」按钮）；number → 数字 TextBox；
/// bool → CheckBox；select → ComboBox（用 options）；password → PasswordBox（不回填）；</item>
/// <item>每项失焦 / 勾选 / 下拉变更，或点「保存」时调用 <c>set_plugin_setting</c>；</item>
/// <item>失败只在字段下方显示红字 <c>[error_code] error_message</c>，不弹全局框、
/// 不改回界面值（用户可修正后重试）；</item>
/// <item>password 值不回填、不缓存字符串，留空表示不修改。</item>
/// </list>
/// </summary>
public partial class SettingsPage
{
    private readonly IPluginSettingsGateway _pluginSettings;
    private readonly List<PluginSettingEditor> _pluginEditors = [];
    private bool _pluginSettingsBusy;

    /// <summary>当前表单里每个字段的绑定（测试锚点：数量 / 控件类型 / 错误文案）。</summary>
    internal IReadOnlyList<PluginSettingEditor> PluginSettingEditors => _pluginEditors;

    /// <summary>按 plugin_id + key 取字段绑定（测试与诊断用）。</summary>
    internal PluginSettingEditor? FindPluginSettingEditor(string pluginId, string key)
    {
        foreach (PluginSettingEditor editor in _pluginEditors)
        {
            if (editor.PluginId == pluginId && editor.Field.Key == key)
            {
                return editor;
            }
        }

        return null;
    }

    /// <summary>
    /// 从 Python 拉一次设置定义并重建表单。失败只渲染空态 + 错误行，不弹窗、不抛异常。
    /// </summary>
    internal async Task RefreshPluginSettingsAsync()
    {
        TextPluginEmpty.Text = "正在加载插件设置…";
        TextPluginEmpty.Visibility = Visibility.Visible;

        PluginSettingsDto result;
        try
        {
            result = await _pluginSettings.ListAsync();
        }
        catch (Exception ex)
        {
            // 网关自身已收口；这里再兜一层，保证页面永远不因设置读取崩掉。
            result = BridgeFailures.Create<PluginSettingsDto>(ex);
        }

        RenderPluginSettings(result);
    }

    /// <summary>按 DTO 重建动态表单（先清空旧控件；失败或没有字段 → 空态）。</summary>
    internal void RenderPluginSettings(PluginSettingsDto? result)
    {
        PanelPluginSettings.Children.Clear();
        _pluginEditors.Clear();

        if (result is null || !result.Ok)
        {
            ShowPluginEmptyState();
            if (result is not null && !result.Ok)
            {
                TextPluginError.Text =
                    $"[{LogSanitizer.Sanitize(result.ErrorCode)}] " +
                    LogSanitizer.Sanitize(result.ErrorMessage);
                TextPluginError.Visibility = Visibility.Visible;
            }

            return;
        }

        List<PluginSettingsGroupDto> groups = (result.Plugins ?? [])
            .Where(group => group.Fields is { Count: > 0 })
            .ToList();
        if (groups.Count == 0)
        {
            ShowPluginEmptyState();
            return;
        }

        TextPluginEmpty.Visibility = Visibility.Collapsed;
        TextPluginError.Text = "";
        TextPluginError.Visibility = Visibility.Collapsed;
        BtnSavePluginSettings.Visibility = Visibility.Visible;

        foreach (PluginSettingsGroupDto group in groups)
        {
            PanelPluginSettings.Children.Add(BuildPluginGroup(group));
        }
    }

    private void ShowPluginEmptyState()
    {
        TextPluginEmpty.Text = "当前没有插件提供设置项";
        TextPluginEmpty.Visibility = Visibility.Visible;
        TextPluginError.Text = "";
        TextPluginError.Visibility = Visibility.Collapsed;
        BtnSavePluginSettings.Visibility = Visibility.Collapsed;
    }

    private FrameworkElement BuildPluginGroup(PluginSettingsGroupDto group)
    {
        var panel = new StackPanel { Margin = new Thickness(0, 0, 0, 6) };

        string title = string.IsNullOrWhiteSpace(group.PluginName)
            ? group.PluginId
            : group.PluginName;
        panel.Children.Add(new TextBlock
        {
            Text = title,
            FontSize = 14,
            FontWeight = FontWeights.SemiBold,
            Foreground = (Brush)FindResource("ColorBrush1"),
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 0, 0, 6),
        });

        foreach (PluginSettingFieldDto field in group.Fields)
        {
            PluginSettingEditor editor = BuildPluginFieldEditor(group.PluginId, field);
            _pluginEditors.Add(editor);
            panel.Children.Add(editor.Root);
        }

        return panel;
    }

    /// <summary>按字段类型建控件；不认识的类型按 text 渲染（Python 侧已限定六种）。</summary>
    private PluginSettingEditor BuildPluginFieldEditor(
        string pluginId,
        PluginSettingFieldDto field)
    {
        var root = new StackPanel { Margin = new Thickness(0, 0, 0, 10) };
        root.Children.Add(new TextBlock
        {
            Text = string.IsNullOrEmpty(field.Label) ? field.Key : field.Label,
            FontSize = 13,
            Foreground = (Brush)FindResource("ColorBrush1"),
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 0, 0, 3),
        });

        var row = new Grid();
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        var controls = new List<Control>();
        FrameworkElement editorControl;
        Button? browseButton = null;
        TextBlock? hint = null;
        // password 从不回填，初始值不参与"是否改动"比较
        string initialJson = field.Value is { } rawValue ? rawValue.GetRawText() : "null";

        switch (field.Type)
        {
            case PluginSettingFieldTypes.Bool:
                var checkBox = new CheckBox
                {
                    // 勾选框语义由上方 label 表达，不硬写"启用"（字段可能是"显示封面"之类）
                    Content = "",
                    IsChecked = PluginSettingValues.ToBool(field.Value),
                    ToolTip = field.Label,
                    MinWidth = 24,
                    VerticalAlignment = VerticalAlignment.Center,
                    MinHeight = 24,
                };
                editorControl = checkBox;
                controls.Add(checkBox);
                break;

            case PluginSettingFieldTypes.Select:
                var comboBox = new ComboBox
                {
                    MinHeight = 32,
                    MinWidth = 160,
                    HorizontalAlignment = HorizontalAlignment.Left,
                    VerticalAlignment = VerticalAlignment.Center,
                };
                string current = PluginSettingValues.ToText(field.Value);
                int selectedIndex = -1;
                foreach (JsonElement option in field.Options ?? [])
                {
                    string optionText = PluginSettingValues.ToText(option);
                    comboBox.Items.Add(new ComboBoxItem
                    {
                        Content = optionText,
                        Tag = PluginSettingValues.OptionToJson(option),
                    });
                    if (selectedIndex < 0 &&
                        string.Equals(optionText, current, StringComparison.Ordinal))
                    {
                        selectedIndex = comboBox.Items.Count - 1;
                    }
                }

                comboBox.SelectedIndex = selectedIndex;
                editorControl = comboBox;
                controls.Add(comboBox);
                break;

            case PluginSettingFieldTypes.Password:
                var passwordBox = new PasswordBox
                {
                    MinHeight = 32,
                    Style = (Style)FindResource("JmlPasswordBox"),
                    VerticalContentAlignment = VerticalAlignment.Center,
                };
                editorControl = passwordBox;
                controls.Add(passwordBox);
                initialJson = "";
                hint = new TextBlock
                {
                    Text = field.HasValue
                        ? "已设置（不会显示原文）；留空表示不修改"
                        : "留空表示不修改",
                    FontSize = 12,
                    Foreground = (Brush)FindResource("ColorBrushGray3"),
                    TextWrapping = TextWrapping.Wrap,
                    Margin = new Thickness(0, 3, 0, 0),
                };
                break;

            case PluginSettingFieldTypes.Number:
                var numberBox = new TextBox
                {
                    MinHeight = 32,
                    Width = 160,
                    HorizontalAlignment = HorizontalAlignment.Left,
                    Style = (Style)FindResource("JmlInputBox"),
                    Text = PluginSettingValues.ToText(field.Value),
                    VerticalContentAlignment = VerticalAlignment.Center,
                };
                if (field.Min is not null || field.Max is not null)
                {
                    numberBox.ToolTip = BuildRangeText(field);
                }

                editorControl = numberBox;
                controls.Add(numberBox);
                break;

            default: // text / path（含未知类型兜底）
                var textBox = new TextBox
                {
                    MinHeight = 32,
                    Style = (Style)FindResource("JmlInputBox"),
                    Text = PluginSettingValues.ToText(field.Value),
                    VerticalContentAlignment = VerticalAlignment.Center,
                };
                editorControl = textBox;
                controls.Add(textBox);
                if (field.Type == PluginSettingFieldTypes.Path)
                {
                    browseButton = new Button
                    {
                        Content = "浏览…",
                        Style = (Style)FindResource("SecondaryButton"),
                        Margin = new Thickness(8, 0, 0, 0),
                        VerticalAlignment = VerticalAlignment.Center,
                    };
                    controls.Add(browseButton);
                }

                break;
        }

        Grid.SetColumn(editorControl, 0);
        row.Children.Add(editorControl);
        if (browseButton is not null)
        {
            Grid.SetColumn(browseButton, 1);
            row.Children.Add(browseButton);
        }

        root.Children.Add(row);
        if (hint is not null)
        {
            root.Children.Add(hint);
        }

        var errorText = new TextBlock
        {
            Visibility = Visibility.Collapsed,
            Margin = new Thickness(0, 3, 0, 0),
            FontSize = 12,
            TextWrapping = TextWrapping.Wrap,
            Foreground = (Brush)FindResource("ColorBrushError"),
        };
        root.Children.Add(errorText);

        var editor = new PluginSettingEditor(
            pluginId,
            field,
            root,
            editorControl,
            errorText,
            controls,
            initialJson);

        AttachCommitHandlers(editor);
        if (browseButton is not null)
        {
            browseButton.Click += (_, _) => BrowseIntoEditor(editor);
        }

        return editor;
    }

    /// <summary>number 字段的悬停提示（从 schema 的 min/max 拼）。</summary>
    private static string BuildRangeText(PluginSettingFieldDto field)
    {
        static string Format(double value) =>
            value.ToString("0.###", CultureInfo.InvariantCulture);

        if (field.Min is { } min && field.Max is { } max)
        {
            return $"允许范围 {Format(min)} ~ {Format(max)}";
        }

        return field.Min is { } onlyMin
            ? $"最小值 {Format(onlyMin)}"
            : $"最大值 {Format(field.Max!.Value)}";
    }

    /// <summary>失焦 / 勾选 / 下拉变更时提交；初始值相等则跳过（不重复写盘）。</summary>
    private void AttachCommitHandlers(PluginSettingEditor editor)
    {
        switch (editor.Field.Type)
        {
            case PluginSettingFieldTypes.Bool:
                var checkBox = (CheckBox)editor.Editor;
                checkBox.Checked += (_, _) => _ = CommitPluginSettingAsync(editor);
                checkBox.Unchecked += (_, _) => _ = CommitPluginSettingAsync(editor);
                break;

            case PluginSettingFieldTypes.Select:
                var comboBox = (ComboBox)editor.Editor;
                comboBox.SelectionChanged += (_, _) => _ = CommitPluginSettingAsync(editor);
                break;

            case PluginSettingFieldTypes.Password:
                var passwordBox = (PasswordBox)editor.Editor;
                passwordBox.LostFocus += (_, _) => _ = CommitPluginSettingAsync(editor);
                break;

            case PluginSettingFieldTypes.Number:
            case PluginSettingFieldTypes.Text:
            case PluginSettingFieldTypes.Path:
            default:
                if (editor.Editor is TextBox textBox)
                {
                    textBox.LostFocus += (_, _) => _ = CommitPluginSettingAsync(editor);
                }

                break;
        }
    }

    /// <summary>「保存」：逐个字段提交（改动过的才发请求；password 留空则跳过）。</summary>
    private async void BtnSavePluginSettings_Click(object sender, RoutedEventArgs e)
    {
        if (_pluginSettingsBusy)
        {
            return;
        }

        _pluginSettingsBusy = true;
        BtnSavePluginSettings.IsEnabled = false;
        try
        {
            await CommitAllPluginSettingsAsync();
        }
        finally
        {
            _pluginSettingsBusy = false;
            BtnSavePluginSettings.IsEnabled = true;
        }
    }

    /// <summary>提交当前表单的全部字段（测试直接调用；未改动的字段不发请求）。</summary>
    internal async Task CommitAllPluginSettingsAsync()
    {
        foreach (PluginSettingEditor editor in _pluginEditors.ToList())
        {
            await CommitPluginSettingAsync(editor);
        }
    }

    /// <summary>
    /// 提交单个字段：控件值 → JSON → <c>set_plugin_setting</c>。
    /// 失败只在字段下方显示 <c>[error_code] error_message</c>，界面值原样保留。
    /// </summary>
    internal async Task CommitPluginSettingAsync(PluginSettingEditor editor)
    {
        if (editor.IsBusy)
        {
            return;
        }

        string? valueJson = BuildValueJson(editor);
        if (valueJson is null)
        {
            // password 留空 = 不修改；顺带清掉上一次的旧错误
            editor.ClearError();
            return;
        }

        if (string.Equals(valueJson, editor.LastSubmittedJson, StringComparison.Ordinal))
        {
            editor.ClearError();
            return;
        }

        editor.SetBusy(true);
        try
        {
            SetPluginSettingResultDto result;
            try
            {
                result = await _pluginSettings.SetAsync(
                    editor.PluginId,
                    editor.Field.Key,
                    valueJson);
            }
            catch (Exception ex)
            {
                result = BridgeFailures.Create<SetPluginSettingResultDto>(ex);
            }

            if (result.Ok)
            {
                editor.LastSubmittedJson = valueJson;
                editor.ClearError();
                if (editor.Field.Type == PluginSettingFieldTypes.Password &&
                    editor.Editor is PasswordBox passwordBox)
                {
                    // 成功写入后立刻清空密码框：C# 不保留任何明文密码，
                    // 之后留空即表示"不修改"。
                    passwordBox.Password = "";
                }
            }
            else
            {
                editor.ShowError(result.ErrorCode, result.ErrorMessage);
            }
        }
        finally
        {
            editor.SetBusy(false);
        }
    }

    /// <summary>按字段类型把控件值转成 JSON 文本；password 留空返回 null（表示不改）。</summary>
    private static string? BuildValueJson(PluginSettingEditor editor)
    {
        switch (editor.Field.Type)
        {
            case PluginSettingFieldTypes.Bool:
                return PluginSettingValues.BoolToJson(
                    ((CheckBox)editor.Editor).IsChecked == true);

            case PluginSettingFieldTypes.Select:
                return ((ComboBox)editor.Editor).SelectedItem is ComboBoxItem item &&
                       item.Tag is string raw
                    ? raw
                    : null;

            case PluginSettingFieldTypes.Password:
                string secret = ((PasswordBox)editor.Editor).Password;
                return string.IsNullOrEmpty(secret)
                    ? null
                    : PluginSettingValues.TextToJson(secret);

            case PluginSettingFieldTypes.Number:
                return PluginSettingValues.NumberToJson(((TextBox)editor.Editor).Text);

            case PluginSettingFieldTypes.Text:
            case PluginSettingFieldTypes.Path:
            default:
                return editor.Editor is TextBox textBox
                    ? PluginSettingValues.TextToJson(textBox.Text)
                    : null;
        }
    }

    private void BrowseIntoEditor(PluginSettingEditor editor)
    {
        if (editor.IsBusy || editor.Editor is not TextBox textBox)
        {
            return;
        }

        var dialog = new OpenFolderDialog
        {
            Title = "选择插件设置目录",
            Multiselect = false,
        };

        if (Directory.Exists(textBox.Text))
        {
            dialog.InitialDirectory = textBox.Text;
        }

        if (dialog.ShowDialog() != true || string.IsNullOrWhiteSpace(dialog.FolderName))
        {
            return;
        }

        textBox.Text = dialog.FolderName;
        _ = CommitPluginSettingAsync(editor);
    }
}

/// <summary>
/// 单个插件设置字段的界面绑定。对外只读：字段定义 + 实际控件 + 错误行，
/// 测试据此断言控件类型、错误文案与“失败不改界面值”。
/// </summary>
internal sealed class PluginSettingEditor
{
    private readonly IReadOnlyList<Control> _controls;

    public PluginSettingEditor(
        string pluginId,
        PluginSettingFieldDto field,
        StackPanel root,
        FrameworkElement editor,
        TextBlock errorText,
        IReadOnlyList<Control> controls,
        string initialJson)
    {
        PluginId = pluginId;
        Field = field;
        Root = root;
        Editor = editor;
        ErrorText = errorText;
        _controls = controls;
        LastSubmittedJson = initialJson;
    }

    /// <summary>所属插件 id。</summary>
    public string PluginId { get; }

    /// <summary>字段定义（类型/标签/选项等）。</summary>
    public PluginSettingFieldDto Field { get; }

    /// <summary>整行容器（标签 + 控件 + 错误行）。</summary>
    public StackPanel Root { get; }

    /// <summary>实际输入控件：TextBox / PasswordBox / CheckBox / ComboBox。</summary>
    public FrameworkElement Editor { get; }

    /// <summary>字段下方的红色错误行（默认折叠）。</summary>
    public TextBlock ErrorText { get; }

    /// <summary>最近一次成功提交的 JSON；用于跳过未改动字段，避免每次失焦都写盘。</summary>
    public string? LastSubmittedJson { get; set; }

    /// <summary>该字段正在写盘（防重入）。</summary>
    public bool IsBusy { get; private set; }

    /// <summary>写盘期间禁用本字段控件（含「浏览…」），避免并发提交同一字段。</summary>
    public void SetBusy(bool busy)
    {
        IsBusy = busy;
        foreach (Control control in _controls)
        {
            control.IsEnabled = !busy;
        }
    }

    /// <summary>清掉错误行（成功后调用）。</summary>
    public void ClearError()
    {
        ErrorText.Text = "";
        ErrorText.Visibility = Visibility.Collapsed;
    }

    /// <summary>
    /// 显示 <c>[error_code] error_message</c>（已脱敏）；不弹全局框。
    /// IO_ERROR 额外补一句当前数据目录，告诉用户“为什么写不进去、去哪里看”，
    /// 同时保留原始 error_code 与 Python 给出的原因。
    /// </summary>
    public void ShowError(string errorCode, string errorMessage)
    {
        string safeCode = LogSanitizer.Sanitize(errorCode);
        string safeMessage = LogSanitizer.Sanitize(errorMessage);
        if (string.Equals(safeCode, "IO_ERROR", StringComparison.Ordinal) &&
            !safeMessage.Contains("数据目录", StringComparison.Ordinal))
        {
            safeMessage =
                "配置写入失败：数据目录不可写" +
                $"（当前数据目录：{LogSanitizer.Sanitize(AppPaths.DataDirectory)}）；" +
                safeMessage;
        }

        ErrorText.Text = $"[{safeCode}] {safeMessage}";
        ErrorText.Visibility = Visibility.Visible;
    }
}
