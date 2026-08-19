param(
    [switch]$Rasterize,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InputPdf
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $appRoot "runtime\python.exe"
$scriptPath = Join-Path $appRoot "app\rasterize.py"
$script:process = $null

function Quote-Argument([string]$value) { return '"' + $value.Replace('"', '\"') + '"' }

$form = New-Object System.Windows.Forms.Form
$form.Text = "図面PDF 全ページ画像化"
$form.Size = New-Object System.Drawing.Size(820, 550)
$form.MinimumSize = New-Object System.Drawing.Size(720, 500)
$form.StartPosition = "CenterScreen"
$form.Font = New-Object System.Drawing.Font("Yu Gothic UI", 9)

$addButton = New-Object System.Windows.Forms.Button
$addButton.Text = "PDFを追加..."
$addButton.Location = New-Object System.Drawing.Point(12, 12)
$addButton.Size = New-Object System.Drawing.Size(110, 30)
$form.Controls.Add($addButton)
$clearButton = New-Object System.Windows.Forms.Button
$clearButton.Text = "すべて消去"
$clearButton.Location = New-Object System.Drawing.Point(128, 12)
$clearButton.Size = New-Object System.Drawing.Size(100, 30)
$form.Controls.Add($clearButton)

$fileList = New-Object System.Windows.Forms.ListBox
$fileList.Location = New-Object System.Drawing.Point(12, 50)
$fileList.Size = New-Object System.Drawing.Size(776, 180)
$fileList.Anchor = "Top,Left,Right"
$fileList.SelectionMode = "MultiExtended"
$fileList.AllowDrop = $true
$form.Controls.Add($fileList)

$outputGroup = New-Object System.Windows.Forms.GroupBox
$outputGroup.Text = "保存先"
$outputGroup.Location = New-Object System.Drawing.Point(12, 240)
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
$outputHint.Text = "空欄なら元PDFと同じフォルダーにPDF名_画像化フォルダーを作成"
$outputHint.Location = New-Object System.Drawing.Point(12, 54)
$outputHint.AutoSize = $true
$outputGroup.Controls.Add($outputHint)

$settings = New-Object System.Windows.Forms.GroupBox
$settings.Text = "ラスタライズ設定"
$settings.Location = New-Object System.Drawing.Point(12, 332)
$settings.Size = New-Object System.Drawing.Size(776, 82)
$settings.Anchor = "Top,Left,Right"
$form.Controls.Add($settings)
$dpiLabel = New-Object System.Windows.Forms.Label
$dpiLabel.Text = "解像度"
$dpiLabel.Location = New-Object System.Drawing.Point(12, 28)
$dpiLabel.AutoSize = $true
$settings.Controls.Add($dpiLabel)
$dpiCombo = New-Object System.Windows.Forms.ComboBox
$dpiCombo.Location = New-Object System.Drawing.Point(70, 24)
$dpiCombo.Size = New-Object System.Drawing.Size(80, 25)
$dpiCombo.DropDownStyle = "DropDownList"
[void]$dpiCombo.Items.AddRange(@("200", "300", "400"))
$dpiCombo.SelectedItem = "300"
$settings.Controls.Add($dpiCombo)
$workersLabel = New-Object System.Windows.Forms.Label
$workersLabel.Text = "並列数"
$workersLabel.Location = New-Object System.Drawing.Point(250, 28)
$workersLabel.AutoSize = $true
$settings.Controls.Add($workersLabel)
$workersCombo = New-Object System.Windows.Forms.ComboBox
$workersCombo.Location = New-Object System.Drawing.Point(320, 24)
$workersCombo.Size = New-Object System.Drawing.Size(80, 25)
$workersCombo.DropDownStyle = "DropDownList"
[void]$workersCombo.Items.AddRange(@("1", "2", "4"))
$workersCombo.SelectedItem = "2"
$settings.Controls.Add($workersCombo)
$formatLabel = New-Object System.Windows.Forms.Label
$formatLabel.Text = "形式"
$formatLabel.Location = New-Object System.Drawing.Point(440, 28)
$formatLabel.AutoSize = $true
$settings.Controls.Add($formatLabel)
$formatCombo = New-Object System.Windows.Forms.ComboBox
$formatCombo.Location = New-Object System.Drawing.Point(490, 24)
$formatCombo.Size = New-Object System.Drawing.Size(90, 25)
$formatCombo.DropDownStyle = "DropDownList"
[void]$formatCombo.Items.AddRange(@("PNG", "TIFF"))
$formatCombo.SelectedItem = "PNG"
$settings.Controls.Add($formatCombo)

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Location = New-Object System.Drawing.Point(12, 430)
$progress.Size = New-Object System.Drawing.Size(555, 25)
$progress.Anchor = "Top,Left,Right"
$form.Controls.Add($progress)
$startButton = New-Object System.Windows.Forms.Button
$startButton.Text = "画像化を開始"
$startButton.Location = New-Object System.Drawing.Point(575, 426)
$startButton.Size = New-Object System.Drawing.Size(125, 32)
$startButton.Anchor = "Top,Right"
$form.Controls.Add($startButton)
$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Text = "中止"
$cancelButton.Location = New-Object System.Drawing.Point(706, 426)
$cancelButton.Size = New-Object System.Drawing.Size(82, 32)
$cancelButton.Anchor = "Top,Right"
$cancelButton.Enabled = $false
$form.Controls.Add($cancelButton)
$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Text = "PDFを追加してください。"
$statusLabel.Location = New-Object System.Drawing.Point(12, 470)
$statusLabel.Size = New-Object System.Drawing.Size(776, 40)
$statusLabel.Anchor = "Top,Left,Right"
$form.Controls.Add($statusLabel)

function Add-PdfFiles([string[]]$paths) {
    foreach ($file in $paths) {
        if ([string]::IsNullOrWhiteSpace($file)) { continue }
        if ((Test-Path -LiteralPath $file -PathType Leaf) -and
            ([System.IO.Path]::GetExtension($file) -ieq ".pdf") -and
            (-not $fileList.Items.Contains($file))) {
            [void]$fileList.Items.Add([System.IO.Path]::GetFullPath($file))
        }
    }
}

$openDialog = New-Object System.Windows.Forms.OpenFileDialog
$openDialog.Filter = "PDF (*.pdf)|*.pdf"
$openDialog.Multiselect = $true
$addButton.Add_Click({ if ($openDialog.ShowDialog() -eq "OK") { Add-PdfFiles $openDialog.FileNames } })
$clearButton.Add_Click({ $fileList.Items.Clear() })
$fileList.Add_DragEnter({
    $_.Effect = if ($_.Data.GetDataPresent([System.Windows.Forms.DataFormats]::FileDrop)) {
        [System.Windows.Forms.DragDropEffects]::Copy
    } else { [System.Windows.Forms.DragDropEffects]::None }
})
$fileList.Add_DragDrop({ Add-PdfFiles @($_.Data.GetData([System.Windows.Forms.DataFormats]::FileDrop)) })
$folderDialog = New-Object System.Windows.Forms.FolderBrowserDialog
$browseButton.Add_Click({ if ($folderDialog.ShowDialog() -eq "OK") { $outputText.Text = $folderDialog.SelectedPath } })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 250
$timer.Add_Tick({
    if ($script:process -and $script:process.HasExited) {
        $timer.Stop(); $progress.Style = "Blocks"; $progress.Value = 100
        $stdout = $script:process.StandardOutput.ReadToEnd()
        $stderr = $script:process.StandardError.ReadToEnd()
        $exitCode = $script:process.ExitCode
        $startButton.Enabled = $true; $cancelButton.Enabled = $false
        $script:process.Dispose(); $script:process = $null
        if ($exitCode -ne 0) {
            $progress.Value = 0; $statusLabel.Text = "エラーで停止しました。"
            [System.Windows.Forms.MessageBox]::Show($stderr, "図面PDF 全ページ画像化", "OK", "Error"); return
        }
        try {
            $results = @((ConvertFrom-Json $stdout).results)
            $pages = ($results | Measure-Object -Property pages -Sum).Sum
            $statusLabel.Text = "完了: $($results.Count)ファイル、$pagesページ"
            $folders = ($results | ForEach-Object { $_.output_dir }) -join "`n"
            [System.Windows.Forms.MessageBox]::Show("$($results.Count)ファイル、$pagesページを画像化しました。`n`n$folders", "図面PDF 全ページ画像化", "OK", "Information")
        } catch { $statusLabel.Text = "完了しましたが、結果表示を解析できませんでした。" }
    }
})

$startButton.Add_Click({
    if ($fileList.Items.Count -eq 0) { [System.Windows.Forms.MessageBox]::Show("処理するPDFを追加してください。", "図面PDF 全ページ画像化"); return }
    if ($outputText.Text -and -not (Test-Path -LiteralPath $outputText.Text -PathType Container)) {
        [System.Windows.Forms.MessageBox]::Show("保存先フォルダーが見つかりません。", "図面PDF 全ページ画像化", "OK", "Error"); return
    }
    $format = if ($formatCombo.SelectedItem -eq "TIFF") { "tif" } else { "png" }
    $arguments = @((Quote-Argument $scriptPath), "--dpi", $dpiCombo.SelectedItem, "--workers", $workersCombo.SelectedItem, "--format", $format)
    if ($outputText.Text) { $arguments += @("--output-dir", (Quote-Argument $outputText.Text)) }
    foreach ($file in $fileList.Items) { $arguments += Quote-Argument ([string]$file) }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $workingDirectory = if ($appRoot.StartsWith("\\")) { $env:windir } else { $appRoot }
    $info.FileName = $pythonExe; $info.Arguments = $arguments -join " "; $info.WorkingDirectory = $workingDirectory
    $info.UseShellExecute = $false; $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [System.Text.Encoding]::UTF8; $info.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $script:process = New-Object System.Diagnostics.Process; $script:process.StartInfo = $info
    [void]$script:process.Start(); $startButton.Enabled = $false; $cancelButton.Enabled = $true
    $progress.Style = "Marquee"; $statusLabel.Text = "全ページを画像化しています。低速PCでは時間がかかります。"; $timer.Start()
})
$cancelButton.Add_Click({ if ($script:process -and -not $script:process.HasExited) { $script:process.Kill(); $statusLabel.Text = "処理を中止しました。" } })
$form.Add_FormClosing({ if ($script:process -and -not $script:process.HasExited) { if ([System.Windows.Forms.MessageBox]::Show("処理中です。中止して終了しますか？", "図面PDF 全ページ画像化", "YesNo", "Question") -eq "Yes") { $script:process.Kill() } else { $_.Cancel = $true } } })
Add-PdfFiles @($InputPdf)
if ($fileList.Items.Count -gt 0) { $statusLabel.Text = "ドロップされたPDFを既定設定で処理します。"; $form.Add_Shown({ $startButton.PerformClick() }) }
if ($env:DRAWING_PDF_OPTIMIZER_SMOKE_TEST -eq "1") {
    if ($env:DRAWING_PDF_OPTIMIZER_SMOKE_RESULT) {
        [System.IO.File]::WriteAllText(
            $env:DRAWING_PDF_OPTIMIZER_SMOKE_RESULT,
            "rasterize-gui=ok files=$($fileList.Items.Count)",
            [System.Text.Encoding]::UTF8
        )
    }
    exit 0
}
[void]$form.ShowDialog()
