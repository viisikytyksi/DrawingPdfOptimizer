# dpo — DrawingPdfOptimizer native engine (Rust)

`app/cli.py` の置き換えを狙った実装。引数・stdout のJSON・stderr の進捗イベント・
Shift_JIS の処理ログを Python 版と同じ形にしてあるので、`gui.ps1` から差し替えられる。

## 状態（2026-10-15 時点・作業中）

- 実装済み: PDF読み書き(lopdf) / 画像XObjectの1bit CCITT G4化 / JPEG・Flate・CCITT デコード /
  SMask合成 / A3上限への縮小 / Pillow互換の2値化パイプライン / 孤立点除去 /
  メタデータ削除 / 未参照オブジェクトの掃除 / 重複画像の統合 /
  処理ログ(Shift_JIS, 実行開始・完了行, 退避先切替) / 進捗イベント
- 実測: 実図面84ページ・画像515点・322MB で **24.0秒**（Python版 55.6秒、同一マシン）
  出力 16.7MB（Python版 15.7MB）
- **画素は完全一致しない**: JPEGデコーダ差（zune-jpeg vs libjpeg）で、合成タイル1.4%、
  実図面のサンプルで約0.16%。libjpeg-turbo を組み込めば縮小できる（`mozjpeg` feature、要 cmake/nasm）
- 未完了: 速度の追加最適化の計測（リサイズの係数表化＋行並列、孤立点除去のビット並列化は実装済みだが
  実図面での効果測定が途中）／Windows exe の実機確認／GUIへの組み込み

## ビルド

```bash
export PATH=$HOME/.cargo/bin:$PATH
cargo build --release                 # Linux
cargo test --release                  # 孤立点除去の単体テスト
cargo build --release --target x86_64-pc-windows-gnu   # Windows exe（要 ~/win-cross）
```

## 主なオプション

`--output-dir` `--dpi` `--contrast` `--threshold` `--no-auto-contrast` `--no-sharpen`
`--denoise` `--include-small` `--keep-metadata` `--progress` `--log-dir`
`--log-fallback-dir` `--fullname` `--jobs` `--timing` `--version`
