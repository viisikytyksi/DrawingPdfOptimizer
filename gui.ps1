param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InputPdf
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $appRoot "runtime\python.exe"
$cliScript = Join-Path $appRoot "app\cli.py"
$script:process = $null
$script:stderrBuffer = [System.Text.StringBuilder]::new()

# Read version from version.py
$appVersion = "0.0.0"
try {
    $versionFile = Join-Path $appRoot "app\version.py"
    $versionLine = Get-Content $versionFile -Raw -Encoding UTF8
    if ($versionLine -match '__version__\s*=\s*["'']([^"'']+)["'']') {
        $appVersion = $Matches[1]
    }
} catch {
    $appVersion = "0.0.0"
}

# NAS log directory (same as 202604端末調査.vbs strNAS)
$script:logDir = "\\10.1.4.110\総室0000\0060 電算関係\★３課PC管理リスト"

# Resolve user full name via WMI (same as 202604端末調査.vbs)
$script:userName = $env:USERNAME
$script:userFullname = ""
try {
    $wmiUser = Get-CimInstance Win32_UserAccount -Filter "Name='$($env:USERNAME)'" -ErrorAction Stop
    if ($wmiUser.FullName) { $script:userFullname = $wmiUser.FullName }
} catch {
    try {
        $netOut = & cmd /c "net user $env:USERNAME /domain" 2``$null
        foreach ($line in $netOut) {
            if ($line -match '全 域  \s*(.+)$') {
                $script:userFullname = $Matches[1].Trim()
                break
            }
        }
    } catch {}
}

# Check NAS accessibility; fall back to app folder if unreachable.
# Re-run before every job so a NAS that dropped after startup is detected.
function Resolve-LogDest {
    try {
        if (-not (Test-Path -LiteralPath $script:logDir -PathType Container -ErrorAction Stop)) {
            throw "NAS not accessible"
        }
        $testFile = Join-Path $script:logDir "_.logtest"
        [System.IO.File]::WriteAllText($testFile, "test", [System.Text.Encoding]::UTF8)
        Remove-Item $testFile -Force
        return $script:logDir
    } catch {
        return $appRoot
    }
}
$script:logDest = Resolve-LogDest

function Quote-Argument([string]$value) {
    return '"' + $value.Replace('\', '\').Replace('"', '\"') + '"'
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "図面PDF 画像2値化 v$appVersion"
$form.Size = New-Object System.Drawing.Size(820, 680)
$form.MinimumSize = New-Object System.Drawing.Size(720, 590)
$form.StartPosition = "CenterScreen"
$form.Font = New-Object System.Drawing.Font("Yu Gothic UI", 9)

$addButton = New-Object System.Windows.Forms.Button
$addButton.Text = "PDFを追加..."
$addButton.Location = New-Object System.Drawing.Point(12, 12)
$addButton.Size = New-Object System.Drawing.Size(110, 30)
$form.Controls.Add($addButton)

$removeButton = New-Object System.Windows.Forms.Button
$removeButton.Text = "選択を削除"
$removeButton.Location = New-Object System.Drawing.Point(128, 12)
$removeButton.Size = New-Object System.Drawing.Size(100, 30)
$form.Controls.Add($removeButton)

$clearButton = New-Object System.Windows.Forms.Button
$clearButton.Text = "すべて消去"
$clearButton.Location = New-Object System.Drawing.Point(234, 12)
$clearButton.Size = New-Object System.Drawing.Size(95, 30)
$form.Controls.Add($clearButton)

$fileList = New-Object System.Windows.Forms.ListBox
$fileList.Location = New-Object System.Drawing.Point(12, 50)
$fileList.Size = New-Object System.Drawing.Size(776, 190)
$fileList.Anchor = "Top,Left,Right"
$fileList.SelectionMode = "MultiExtended"
$fileList.AllowDrop = $true
$form.Controls.Add($fileList)

$outputGroup = New-Object System.Windows.Forms.GroupBox
$outputGroup.Text = "保存先"
$outputGroup.Location = New-Object System.Drawing.Point(12, 250)
$outputGroup.Size = New-Object System.Drawing.Size(776, 82)
$outputGroup.Anchor = "Top,Left,Right"
$form.Controls.Add($outputGroup)

$outputText = New-Object System.Windows.Forms.TextBox
$outputText.Location = New-Object System.Drawing.Point(12, 24)
$outputText.Size = New-Object System.Drawing.Size(660, 25)
$outputText.Anchor = "Top,Left,Right"
$outputGroup.Controls.Add($outputText)

$browseButton = New-Object System.Windows.Forms.Button
$browseButton.Text = "参照..."
$browseButton.Location = New-Object System.Drawing.Point(680, 21)
$browseButton.Size = New-Object System.Drawing.Size(82, 28)
$browseButton.Anchor = "Top,Right"
$outputGroup.Controls.Add($browseButton)

$outputHint = New-Object System.Windows.Forms.Label
$outputHint.Text = "空欄なら元PDFと同じフォルダーへ別名保存"
$outputHint.Location = New-Object System.Drawing.Point(12, 54)
$outputHint.AutoSize = $true
$outputGroup.Controls.Add($outputHint)

$settings = New-Object System.Windows.Forms.GroupBox
$settings.Text = "画像処理"
$settings.Location = New-Object System.Drawing.Point(12, 342)
$settings.Size = New-Object System.Drawing.Size(776, 190)
$settings.Anchor = "Top,Left,Right"
$form.Controls.Add($settings)

$dpiLabel = New-Object System.Windows.Forms.Label
$dpiLabel.Text = "A3印刷解像度"
$dpiLabel.Location = New-Object System.Drawing.Point(12, 28)
$dpiLabel.AutoSize = $true
$settings.Controls.Add($dpiLabel)

$dpiCombo = New-Object System.Windows.Forms.ComboBox
$dpiCombo.Location = New-Object System.Drawing.Point(115, 24)
$dpiCombo.Size = New-Object System.Drawing.Size(80, 25)
$dpiCombo.DropDownStyle = "DropDownList"
[void]$dpiCombo.Items.AddRange(@("200", "300", "400"))
$dpiCombo.SelectedItem = "300"
$settings.Controls.Add($dpiCombo)

$dpiHint = New-Object System.Windows.Forms.Label
$dpiHint.Text = "dpi（300推奨）"
$dpiHint.Location = New-Object System.Drawing.Point(202, 28)
$dpiHint.AutoSize = $true
$settings.Controls.Add($dpiHint)

$autoContrast = New-Object System.Windows.Forms.CheckBox
$autoContrast.Text = "自動コントラスト"
$autoContrast.Location = New-Object System.Drawing.Point(350, 25)
$autoContrast.AutoSize = $true
$autoContrast.Checked = $true
$settings.Controls.Add($autoContrast)

$sharpen = New-Object System.Windows.Forms.CheckBox
$sharpen.Text = "線を明瞭化"
$sharpen.Location = New-Object System.Drawing.Point(520, 25)
$sharpen.AutoSize = $true
$sharpen.Checked = $true
$settings.Controls.Add($sharpen)

$contrastLabel = New-Object System.Windows.Forms.Label
$contrastLabel.Text = "コントラスト"
$contrastLabel.Location = New-Object System.Drawing.Point(12, 70)
$contrastLabel.AutoSize = $true
$settings.Controls.Add($contrastLabel)

$contrastTrack = New-Object System.Windows.Forms.TrackBar
$contrastTrack.Location = New-Object System.Drawing.Point(105, 58)
$contrastTrack.Size = New-Object System.Drawing.Size(210, 45)
$contrastTrack.Minimum = 100
$contrastTrack.Maximum = 150
$contrastTrack.Value = 115
$contrastTrack.TickFrequency = 10
$settings.Controls.Add($contrastTrack)

$thresholdLabel = New-Object System.Windows.Forms.Label
$thresholdLabel.Text = "黒を増減"
$thresholdLabel.Location = New-Object System.Drawing.Point(350, 70)
$thresholdLabel.AutoSize = $true
$settings.Controls.Add($thresholdLabel)

$thresholdTrack = New-Object System.Windows.Forms.TrackBar
$thresholdTrack.Location = New-Object System.Drawing.Point(425, 58)
$thresholdTrack.Size = New-Object System.Drawing.Size(240, 45)
$thresholdTrack.Minimum = -30
$thresholdTrack.Maximum = 30
$thresholdTrack.Value = 0
$thresholdTrack.TickFrequency = 10
$settings.Controls.Add($thresholdTrack)

$includeSmall = New-Object System.Windows.Forms.CheckBox
$includeSmall.Text = "小さい画像（ロゴ・印影など）も2値化する"
$includeSmall.Location = New-Object System.Drawing.Point(12, 118)
$includeSmall.AutoSize = $true
$settings.Controls.Add($includeSmall)

$stripMetadata = New-Object System.Windows.Forms.CheckBox
$stripMetadata.Text = "PDFメタデータを削除（公告掲載向け）"
$stripMetadata.Location = New-Object System.Drawing.Point(350, 118)
$stripMetadata.AutoSize = $true
$stripMetadata.Checked = $true
$settings.Controls.Add($stripMetadata)

$denoise = New-Object System.Windows.Forms.CheckBox
$denoise.Text = "ノイズ除去（薄いスキャン向け・時間約1.5倍）"
$denoise.Location = New-Object System.Drawing.Point(12, 148)
$denoise.AutoSize = $true
$denoise.Checked = $true
$settings.Controls.Add($denoise)

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Location = New-Object System.Drawing.Point(12, 548)
$progress.Size = New-Object System.Drawing.Size(555, 25)
$progress.Anchor = "Top,Left,Right"
$form.Controls.Add($progress)

$startButton = New-Object System.Windows.Forms.Button
$startButton.Text = "最適化を開始"
$startButton.Location = New-Object System.Drawing.Point(575, 544)
$startButton.Size = New-Object System.Drawing.Size(125, 32)
$startButton.Anchor = "Top,Right"
$form.Controls.Add($startButton)

$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Text = "中止"
$cancelButton.Location = New-Object System.Drawing.Point(706, 544)
$cancelButton.Size = New-Object System.Drawing.Size(82, 32)
$cancelButton.Anchor = "Top,Right"
$cancelButton.Enabled = $false
$form.Controls.Add($cancelButton)

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Text = "PDFを追加してください。"
$statusLabel.Location = New-Object System.Drawing.Point(12, 587)
$statusLabel.Size = New-Object System.Drawing.Size(776, 40)
$statusLabel.Anchor = "Top,Left,Right"
$form.Controls.Add($statusLabel)

$openDialog = New-Object System.Windows.Forms.OpenFileDialog
$openDialog.Filter = "PDF (*.pdf)|*.pdf"
$openDialog.Multiselect = $true

function Add-PdfFiles([string[]]$paths) {
    foreach ($file in $paths) {
        if ([string]::IsNullOrWhiteSpace($file)) { continue }
        if (
            (Test-Path -LiteralPath $file -PathType Leaf) -and
            ([System.IO.Path]::GetExtension($file) -ieq ".pdf") -and
            (-not $fileList.Items.Contains($file))
        ) {
            [void]$fileList.Items.Add([System.IO.Path]::GetFullPath($file))
        }
    }
}

$addButton.Add_Click({
    if ($openDialog.ShowDialog() -eq "OK") {
        Add-PdfFiles $openDialog.FileNames
    }
})
$removeButton.Add_Click({
    $selected = @($fileList.SelectedItems)
    foreach ($item in $selected) { $fileList.Items.Remove($item) }
})
$clearButton.Add_Click({ $fileList.Items.Clear() })

$dragEnterHandler = {
    if ($_.Data.GetDataPresent([System.Windows.Forms.DataFormats]::FileDrop)) {
        $pdfFound = @($_.Data.GetData([System.Windows.Forms.DataFormats]::FileDrop)) |
            Where-Object { [System.IO.Path]::GetExtension($_) -ieq ".pdf" } |
            Select-Object -First 1
        $_.Effect = if ($pdfFound) {
            [System.Windows.Forms.DragDropEffects]::Copy
        } else {
            [System.Windows.Forms.DragDropEffects]::None
        }
    }
}
$dragDropHandler = {
    Add-PdfFiles @($_.Data.GetData([System.Windows.Forms.DataFormats]::FileDrop))
}
$fileList.Add_DragEnter($dragEnterHandler)
$fileList.Add_DragDrop($dragDropHandler)

$folderDialog = New-Object System.Windows.Forms.FolderBrowserDialog
$browseButton.Add_Click({
    if ($folderDialog.ShowDialog() -eq "OK") { $outputText.Text = $folderDialog.SelectedPath }
})

$script:logWarnPath = ""
$script:logWarnError = ""
$script:logMovedPath = ""
$script:fileErrors = @()

$script:progressLine = { param([string]$line)
    if ([string]::IsNullOrWhiteSpace($line)) { return }
    try {
        $ev = $line | ConvertFrom-Json
        switch ($ev.type) {
            "file_start" {
                $statusLabel.Text = "[$($ev.file)/$($ev.total_files)] $($ev.name) を読み込み中..."
            }
            "progress" {
                $fileFrac = ($ev.file - 1 + $ev.fraction) / $ev.total_files
                $progress.Style = "Blocks"
                $progress.Value = [int]($fileFrac * 100)
                $imgInfo = if ($ev.total_images -gt 0) { " 画像 $([Math]::Min($ev.image + 1, $ev.total_images))/$($ev.total_images)" } else { "" }
                $statusLabel.Text = "[$($ev.file)/$($ev.total_files)] $($ev.name): $($ev.message)$imgInfo"
            }
            "file_done" {
                $fileFrac = $ev.file / $ev.total_files
                $progress.Value = [int]($fileFrac * 100)
                $statusLabel.Text = "[$($ev.file)/$($ev.total_files)] $($ev.name) 完了"
            }
            "all_done" {
                $progress.Value = 100
            }
            "file_error" {
                $script:fileErrors += "$($ev.name): $($ev.error)"
                $statusLabel.Text = "[$($ev.file)/$($ev.total_files)] $($ev.name) 失敗: $($ev.error)"
            }
            "log_moved" {
                $script:logMovedPath = [string]$ev.path
                $statusLabel.Text = "警告: ログの保存先を変更しました（$($ev.path)）"
            }
            "log_error" {
                if (-not $script:logWarnError) {
                    $script:logWarnPath = [string]$ev.path
                    $script:logWarnError = [string]$ev.error
                }
                $statusLabel.Text = "警告: 処理ログを保存できません（$($ev.error)）"
            }
        }
    } catch {}
}

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 250
$timer.Add_Tick({
    # Read available stderr lines for progress
    if ($script:process -and -not $script:process.StandardError.EndOfStream) {
        while (-not $script:process.StandardError.EndOfStream) {
            $line = $script:process.StandardError.ReadLine()
            if ($null -eq $line) { break }
            $progressLine.Invoke($line)
        }
    }
    if ($script:process -and $script:process.HasExited) {
        $timer.Stop()
        # Drain remaining stderr
        while (-not $script:process.StandardError.EndOfStream) {
            $line = $script:process.StandardError.ReadLine()
            if ($null -eq $line) { break }
            $progressLine.Invoke($line)
        }
        $progress.Style = "Blocks"
        $progress.Value = 100
        $stdout = $script:process.StandardOutput.ReadToEnd()
        $stderr = $script:process.StandardError.ReadToEnd()
        $exitCode = $script:process.ExitCode
        $startButton.Enabled = $true
        $cancelButton.Enabled = $false
        $script:process.Dispose()
        $script:process = $null
        if ($exitCode -ne 0) {
            $progress.Value = 0
            $statusLabel.Text = "エラーで停止しました。"
            $errText = (($stderr -split "`r?`n") | Where-Object { $_.Trim() -ne "" -and -not $_.TrimStart().StartsWith("{") }) -join "`n"
            if (-not $errText) {
                $errText = if ($script:fileErrors.Count -gt 0) { $script:fileErrors -join "`n" } else { "不明なエラー" }
            }
            [System.Windows.Forms.MessageBox]::Show($errText, "図面PDF 画像2値化 v$appVersion", "OK", "Error")
            return
        }
        try {
            $data = $stdout | ConvertFrom-Json
            $results = @($data.results)
            $converted = ($results | Measure-Object -Property converted_images -Sum).Sum
            $before = ($results | Measure-Object -Property bytes_before -Sum).Sum
            $after = ($results | Measure-Object -Property bytes_after -Sum).Sum
            $percent = if ($before -gt 0) { [Math]::Round((1 - $after / $before) * 100) } else { 0 }
            $statusLabel.Text = "完了: $($results.Count)ファイル、$converted 画像を2値化"
            $message = "$($results.Count)ファイルを保存しました。`n2値化した画像: $converted`n容量: $([Math]::Round($before / 1MB, 1)) MB → $([Math]::Round($after / 1MB, 1)) MB（$percent%削減）"
            $warnings = @()
            if ($script:fileErrors.Count -gt 0) {
                $warnings += "【失敗】$($script:fileErrors.Count)件のファイルを処理できませんでした。`n" + (($script:fileErrors | ForEach-Object { "・$_" }) -join "`n")
                $statusLabel.Text = "完了（$($script:fileErrors.Count)件失敗）: $($results.Count)ファイル、$converted 画像を2値化"
            }
            if ($script:logMovedPath) {
                $warnings += "【注意】処理ログの保存先を変更しました。`n新しい保存先: $($script:logMovedPath)"
            }
            if ($script:logWarnError) {
                $warnings += "【警告】処理ログを保存できませんでした。`n保存先: $($script:logWarnPath)`nエラー: $($script:logWarnError)`nこの処理の記録は残っていません。配布元へ連絡してください。"
            }
            if ($warnings.Count -gt 0) {
                [System.Windows.Forms.MessageBox]::Show(($warnings -join "`n`n") + "`n`n" + $message, "図面PDF 画像2値化 v$appVersion", "OK", "Warning")
            } else {
                [System.Windows.Forms.MessageBox]::Show($message, "図面PDF 画像2値化 v$appVersion", "OK", "Information")
            }
        } catch {
            $statusLabel.Text = "保存は完了しましたが、結果表示を解析できませんでした。"
        }
    }
})

$startAction = {
    if ($fileList.Items.Count -eq 0) {
        [System.Windows.Forms.MessageBox]::Show("処理するPDFを追加してください。", "図面PDF 画像2値化")
        return
    }
    if ($outputText.Text -and -not (Test-Path -LiteralPath $outputText.Text -PathType Container)) {
        [System.Windows.Forms.MessageBox]::Show("保存先フォルダーが見つかりません。", "図面PDF 画像2値化", "OK", "Error")
        return
    }

    $script:logWarnPath = ""
    $script:logWarnError = ""
    $script:logMovedPath = ""
    $script:fileErrors = @()
    $script:logDest = Resolve-LogDest

    $arguments = @((Quote-Argument $cliScript), "--progress",
        "--log-dir", (Quote-Argument $script:logDest),
        "--log-fallback-dir", (Quote-Argument $appRoot),
        "--fullname", (Quote-Argument $script:userFullname),
        "--dpi", $dpiCombo.SelectedItem, "--contrast", ($contrastTrack.Value / 100.0), "--threshold", $thresholdTrack.Value)
    if ($outputText.Text) { $arguments += @("--output-dir", (Quote-Argument $outputText.Text)) }
    if (-not $autoContrast.Checked) { $arguments += "--no-auto-contrast" }
    if (-not $sharpen.Checked) { $arguments += "--no-sharpen" }
    if ($includeSmall.Checked) { $arguments += "--include-small" }
    if ($denoise.Checked) { $arguments += "--denoise" }
    if (-not $stripMetadata.Checked) { $arguments += "--keep-metadata" }
    foreach ($file in $fileList.Items) { $arguments += Quote-Argument ([string]$file) }

    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $pythonExe
    $info.Arguments = $arguments -join " "
    $info.WorkingDirectory = $appRoot
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $script:process = New-Object System.Diagnostics.Process
    $script:process.StartInfo = $info
    [void]$script:process.Start()
    $startButton.Enabled = $false
    $cancelButton.Enabled = $true
    $progress.Style = "Blocks"
    $progress.Value = 0
    $statusLabel.Text = "PDFを読み込み中..."
    $timer.Start()
}
$startButton.Add_Click($startAction)

$cancelButton.Add_Click({
    if ($script:process -and -not $script:process.HasExited) {
        $script:process.Kill()
        $statusLabel.Text = "処理を中止しました。"
    }
})

$form.Add_FormClosing({
    if ($script:process -and -not $script:process.HasExited) {
        $answer = [System.Windows.Forms.MessageBox]::Show("処理中です。中止して終了しますか？", "図面PDF 画像2値化", "YesNo", "Question")
        if ($answer -ne "Yes") { $_.Cancel = $true; return }
        $script:process.Kill()
    }
})

Add-PdfFiles @($InputPdf)
$script:autoStart = $fileList.Items.Count -gt 0
if ($script:autoStart) {
    $statusLabel.Text = "ドロップされたPDFを既定設定で処理します。"
    $form.Add_Shown({ $startButton.PerformClick() })
}

if ($env:DRAWING_PDF_OPTIMIZER_SMOKE_TEST -eq "1") {
    $smokeResult = "gui=ok files=$($fileList.Items.Count) autostart=$script:autoStart"
    if ($env:DRAWING_PDF_OPTIMIZER_SMOKE_RESULT) {
        [System.IO.File]::WriteAllText(
            $env:DRAWING_PDF_OPTIMIZER_SMOKE_RESULT,
            $smokeResult,
            [System.Text.Encoding]::UTF8
        )
    }
    Write-Output $smokeResult
    exit 0
}
[void]$form.ShowDialog()
