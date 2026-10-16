# dpo — DrawingPdfOptimizer native engine (Rust)

`app/cli.py` の置き換えを狙った実装。引数・stdout のJSON・stderr の進捗イベント・
Shift_JIS の処理ログを Python 版と同じ形にしてあるので、`gui.ps1` から差し替えられる。

## 状態（2026-10-16 時点）

- 実装済み: PDF読み書き(lopdf) / 画像XObjectの1bit CCITT G4化 / JPEG・Flate・CCITT デコード /
  SMask合成 / A3上限への縮小 / Pillow互換の2値化パイプライン / 孤立点除去 /
  メタデータ削除 / 未参照オブジェクトの掃除 / 重複画像の統合 /
  処理ログ(Shift_JIS, 実行開始・完了行, 退避先切替) / 進捗イベント
- 実測（Windows11 i5-8400T、x86-64-v3ビルド、実図面84ページ/画像515点/322MB/ノイズ除去あり）:
  **21.6〜22.0秒**。Python版(v0.8.2、同機 CPython 3.12.10 + pypdf 6.10.0 / pillow 12.2.0固定)は
  100.1〜101.1秒で比較すると **約4.6倍速**。出力 13,839,239バイト
  （Python版は15,715,547バイト、職場PC/m720qの両記録とバイト数一致）
- **画素の一致**: Python版との完全一致はJPEGデコーダ差（zune-jpeg vs libjpeg）で成り立たない
  （m720q実測: 合成タイル1.4%、実図面サンプル0.16%）。**コード・target-cpuの組合せ内では
  出力はビットで安定**（x86-64-v3 有無で同SHA256を確認）
- 未完了: Windows実機(職場PC)確認 / GUI組み込み（`gui.ps1`は`dpo.exe`優先呼び出しに改修予定）

## ビルド

`.cargo/config.toml` で `-C target-cpu=x86-64-v3`（AVX2/FMA他）を固定してある。
**配布先・実行環境は x86-64-v3 対応CPU（2013年以降のIntel/AMD）が前提**。
AVX2を持たないCPUでは起動時に「AVX2(x86-64-v3)対応CPUが必要」で停止する。

```bash
export PATH=$HOME/.cargo/bin:$PATH
cargo build --release                 # Linux (m720q などの v3 CPU で実行)
cargo test --release                  # 3件の単体テスト
cargo build --release --target x86_64-pc-windows-gnu   # Windows exe（要 ~/win-cross）
```

Windows(自己完結GNUツールチェイン)での初回ビルドは, dlltool がgas(as)を併用するため,
`binutilsの bin/ld` もしくは `w64devkit/bin` と `rustlib/<target>/bin` に dlltool.exe と
`x86_64-w64-mingw32-as.exe`(実体の as.exe と同義, binutilsのasと) を置くだけでビルドが通る。
その詳細は `drawing-pdf-optimizer.md`(サーバー側 vault) を参照。

## 主なオプション

`--output-dir` `--dpi` `--contrast` `--threshold` `--no-auto-contrast` `--no-sharpen`
`--denoise` `--include-small` `--keep-metadata` `--progress` `--log-dir`
`--log-fallback-dir` `--fullname` `--jobs` `--resize lanczos|bilinear|bicubic|nearest`（既定lanczos・nearest等は比較実験用） `--timing` `--version`
