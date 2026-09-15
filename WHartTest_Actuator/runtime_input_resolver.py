import argparse
import contextlib
import io
import json
import re
from pathlib import Path
from typing import Any, Callable


def normalize_ocr_value(value: Any) -> str:
    text = re.sub(r'\s+', '', str(value or ''))
    return ''.join(character for character in text if character.isalnum())


def resolve_image_captcha(
    image_path: str | Path,
    classifier_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f'验证码图片不存在: {path}')

    if classifier_factory is None:
        import ddddocr

        classifier_factory = lambda: ddddocr.DdddOcr(show_ad=False)

    initialization_output = io.StringIO()
    with contextlib.redirect_stdout(initialization_output):
        classifier = classifier_factory()
        raw_value = classifier.classification(path.read_bytes())

    value = normalize_ocr_value(raw_value)
    if not value:
        raise ValueError('OCR 未识别出有效验证码')
    return {
        'status': 'success',
        'resolver': 'image_ocr',
        'value': value,
        'raw_value': str(raw_value or ''),
        'image_path': str(path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='WHartTest runtime input resolver')
    parser.add_argument('--resolver', default='image_ocr')
    parser.add_argument('--image', required=True)
    args = parser.parse_args()

    try:
        if args.resolver != 'image_ocr':
            raise ValueError(f'不支持的运行时解析器: {args.resolver}')
        result = resolve_image_captcha(args.image)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({
            'status': 'failed',
            'resolver': args.resolver,
            'error': f'{type(exc).__name__}: {exc}',
        }, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
