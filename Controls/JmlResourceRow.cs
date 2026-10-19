using System.Windows.Input;
using System.Windows.Media;

namespace JML.Controls;

// PCL2 资源列表行（MyResourceItem）的展示模型。
// 来源：PCL-main\Plain Craft Launcher 2\Pages\PageDownload\Resource\MyResourceItem.xaml
// 这里只放"展示用"数据，不含任何业务判断；图标路径与 PCL2 原版一致。

/// <summary>资源行底部信息项：一个 11px 图标 + 一段 12px 灰字。</summary>
public sealed record JmlResourceMetaItem(Geometry Icon, string Text);

/// <summary>资源行右侧操作按钮。</summary>
public sealed record JmlResourceAction(string Title, ICommand Command);

/// <summary>最小的命令实现：资源行按钮只需要"点一下执行一个回调"。</summary>
public sealed class JmlDelegateCommand : ICommand
{
    private readonly Action<object?> _execute;

    public JmlDelegateCommand(Action<object?> execute)
    {
        _execute = execute ?? throw new ArgumentNullException(nameof(execute));
    }

    // 资源行按钮的可用性由页面统一控制（单飞、忙碌态），这里始终可执行。
    public bool CanExecute(object? parameter) => true;

    public void Execute(object? parameter) => _execute(parameter);

    public event EventHandler? CanExecuteChanged
    {
        add { }
        remove { }
    }
}

/// <summary>资源行底部信息图标（路径数据逐字取自 PCL2 MyResourceItem.xaml）。</summary>
public static class JmlResourceIcons
{
    /// <summary>版本 / 编号（PCL2 PathVersion）。</summary>
    public static Geometry Version { get; } = Create(
        "M940.4 463.7L773.3 174.2c-17.3-30-49.2-48.4-83.8-48.4H340.2c-34.6 0-66.5 18.5-83.8 48.4L89.2 463.7c-17.3 30-17.3 66.9 0 96.8L256.4 850c17.3 30 49.2 48.4 83.8 48.4h349.2c34.6 0 66.5-18.5 83.8-48.4l167.2-289.5c17.3-29.9 17.3-66.8 0-96.8z m-94.6 96.8L725.9 768.1c-17.3 30-49.2 48.4-83.8 48.4H387.5c-34.6 0-66.5-18.5-83.8-48.4L183.9 560.5c-17.3-30-17.3-66.9 0-96.8l119.8-207.5c17.3-30 49.2-48.4 83.8-48.4h254.6c34.6 0 66.5 18.5 83.8 48.4l119.8 207.5c17.3 30 17.3 66.9 0.1 96.8z M522.3 321.2c-2.5-0.1-5-0.2-7.5-0.2-119.9 0-214 110.3-186.3 235 15.8 70.9 71.5 126.6 142.4 142.4 17.5 3.9 34.7 5.4 51.4 4.7 102.1-3.9 183.6-87.9 183.6-191 0.1-103-81.5-187-183.6-190.9z m68.6 269.1c-18.5 18-43 28.9-68.6 30.7l-6 0.3c-30.2 0.4-58.6-11.4-79.7-33-19.5-20.1-30.7-47-30.9-75-0.3-29.6 11.1-57.4 32-78.3 20.6-20.6 48-32 77.2-32 2.5 0 5 0.1 7.5 0.3 26.7 1.8 51.5 13.2 70.5 32.5 19.6 20 30.8 46.9 31.2 74.9 0.2 30.2-11.5 58.6-33.2 79.6z");

    /// <summary>下载量（PCL2 PathDownload）。</summary>
    public static Geometry Download { get; } = Create(
        "M955 610h-59c-15 0-29 13-29 29v196c0 15-13 29-29 29h-649c-15 0-29-13-29-29v-196c0-15-13-29-29-29h-59c-15 0-29 13-29 29V905c0 43 35 78 78 78h787c43 0 78-35 78-78V640c0-15-13-29-29-29zM492 740c11 11 29 11 41 0l265-265c11-11 11-29 0-41l-41-41c-11-11-29-11-41 0l-110 110c-11 11-33 3-33-13V68C571 53 555 39 541 39h-59c-15 0-29 13-29 29v417c0 17-21 25-33 13l-110-110c-11-11-29-11-41 0L226 433c-11 11-11 29 0 41L492 740z");

    /// <summary>时间（PCL2 PathTime）。</summary>
    public static Geometry Time { get; } = Create(
        "M810 403l-39 39c-10 10-28 10-39 0l-162-162v412c0 15-12 28-28 28h-56c-15 0-28-12-28-28v-412l-162 162c-10 10-28 10-39 0l-39-39c-10-10-10-28 0-39l278-278c10-10 28-10 39 0l278 278c10 10 10 28-0 39zM568 946h-112v-112h112v112z");

    /// <summary>来源（PCL2 PathSource）。</summary>
    public static Geometry Source { get; } = Create(
        "M511 143c-203 0-369 165-369 369 0 203 165 369 369 369s369-165 369-369c0-203-165-369-369-369z m0 673c-29 0-68-59-89-159l178 0c-20 100-59 159-89 159z m-98-224c-2-24-3-51-3-79 0-29 1-57 4-83l197 0c2 25 4 53 4 83 0 28-1 54-3 79l-198-0z m98-383c29 0 67 58 88 156l-177-0c20-98 59-155 88-155z m164 221l128 0c7 26 11 54 11 82 0 27-3 54-10 80l-129-0c2-26 3-52 3-79 0-28-1-56-4-83z m101-64l-110-0c-7-37-16-72-28-102-4-11-9-22-15-33 66 25 121 74 155 135z m-376-135c-5 10-10 21-15 33-12 30-21 64-28 102l-110-0c34-60 89-109 154-134z m-181 199l128 0c-2 27-3 54-3 83 0 27 1 53 3 79l-127-0v6c-8-27-12-55-12-85-0-29 3-57 11-83z m24 227l112 0c7 38 16 74 29 105 4 11 9 22 15 33-67-26-122-76-156-139zM621 795c5-10 10-21 15-33 12-30 22-66 29-105l111 0c-34 62-89 111-156 137z");

    private static Geometry Create(string data)
    {
        try
        {
            Geometry geometry = Geometry.Parse(data);
            geometry.Freeze();
            return geometry;
        }
        catch (FormatException)
        {
            // 图标只是装饰：路径数据异常时退回空图标，不能让整个列表崩掉。
            return Geometry.Empty;
        }
    }
}
