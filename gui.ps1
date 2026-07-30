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

function Quote-Argument([string]$value) {
    return '"' + $value.Replace('\', '\').Replace('"', '\"') + '"'
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "図面PDF 画像2値化"
$form.Size = New-Object System.Drawing.Size(820, 650)
$form.MinimumSize = New-Object System.Drawing.Size(720, 560)
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
$settings.Size = New-Object System.Drawing.Size(776, 160)
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

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Location = New-Object System.Drawing.Point(12, 518)
$progress.Size = New-Object System.Drawing.Size(555, 25)
$progress.Anchor = "Top,Left,Right"
$form.Controls.Add($progress)

$startButton = New-Object System.Windows.Forms.Button
$startButton.Text = "最適化を開始"
$startButton.Location = New-Object System.Drawing.Point(575, 514)
$startButton.Size = New-Object System.Drawing.Size(125, 32)
$startButton.Anchor = "Top,Right"
$form.Controls.Add($startButton)

$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Text = "中止"
$cancelButton.Location = New-Object System.Drawing.Point(706, 514)
$cancelButton.Size = New-Object System.Drawing.Size(82, 32)
$cancelButton.Anchor = "Top,Right"
$cancelButton.Enabled = $false
$form.Controls.Add($cancelButton)

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Text = "PDFを追加してください。"
$statusLabel.Location = New-Object System.Drawing.Point(12, 557)
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

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 250
$timer.Add_Tick({
    if ($script:process -and $script:process.HasExited) {
        $timer.Stop()
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
            [System.Windows.Forms.MessageBox]::Show($stderr, "図面PDF 画像2値化", "OK", "Error")
            return
        }
        try {
            $data = $stdout | ConvertFrom-Json
            $results = @($data.results)
            $converted = ($results | Measure-Object -Property converted_images -Sum).Sum
            $before = ($results | Measure-Object -Property bytes_before -Sum).Sum
            $after = ($results | Measure-Object -Property bytes_after -Sum).Sum
            $percent = if ($before -gt 0) { [Math]::Round((1 - $after / $before) * 100) } else { 0 }
            $metadataStripped = ($results | Where-Object { $_.metadata_stripped } | Measure-Object).Count
            $statusLabel.Text = "完了: $($results.Count)ファイル、$converted 画像を2値化"
            $message = "$($results.Count)ファイルを保存しました。`n2値化した画像: $converted`nメタデータ削除: $metadataStripped ファイル`n容量: $([Math]::Round($before / 1MB, 1)) MB → $([Math]::Round($after / 1MB, 1)) MB（$percent%削減）"
            [System.Windows.Forms.MessageBox]::Show($message, "図面PDF 画像2値化", "OK", "Information")
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

    $arguments = @((Quote-Argument $cliScript), "--dpi", $dpiCombo.SelectedItem, "--contrast", ($contrastTrack.Value / 100.0), "--threshold", $thresholdTrack.Value)
    if ($outputText.Text) { $arguments += @("--output-dir", (Quote-Argument $outputText.Text)) }
    if (-not $autoContrast.Checked) { $arguments += "--no-auto-contrast" }
    if (-not $sharpen.Checked) { $arguments += "--no-sharpen" }
    if ($includeSmall.Checked) { $arguments += "--include-small" }
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
    $progress.Style = "Marquee"
    $statusLabel.Text = "画像を処理しています。大きなPDFでは数分かかります。"
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
