"""ローカル確認の起動口。通常の収集・配信処理はcore.pyにあります。"""

from local_check import main

if __name__ == "__main__":
    raise SystemExit(main())
