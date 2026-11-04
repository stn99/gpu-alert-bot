param(
    [string]$Title = "GPU Alert Bot",
    [string]$Body = "",
    [string]$Url = ""
)
# Windows 10/11 toast notification without extra modules. Clicking it opens $Url.
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]

function Esc([string]$s) { return [System.Security.SecurityElement]::Escape($s) }

$launch = ""
if ($Url) { $launch = ' activationType="protocol" launch="' + (Esc $Url) + '"' }
$xmlText = '<toast' + $launch + ' duration="long"><visual><binding template="ToastGeneric"><text>' + (Esc $Title) + '</text><text>' + (Esc $Body) + '</text></binding></visual><audio src="ms-winsoundevent:Notification.Reminder"/></toast>'

$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($xmlText)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
