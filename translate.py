#!/usr/bin/env python3
"""翻译工具：免费翻译源（有道 jsonapi_s + MyMemory 兜底）"""

import logging
import requests

logger = logging.getLogger('searchbot.translate')

MYMEMORY_EMAILS = ['searchbot@outlook.com', 'searchbot@gmail.com', 'searchbot@qq.com']


def _youdao(text: str) -> str:
    """有道翻译免费接口（dict.youdao.com/jsonapi_s）"""
    r = requests.get(
        'https://dict.youdao.com/jsonapi_s',
        params={'jsonversion': '2', 'client': 'mobile', 'q': text},
        timeout=12,
        headers={'User-Agent': 'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36'},
    )
    r.raise_for_status()
    data = r.json()
    fanyi = data.get('fanyi') or {}
    tran = str(fanyi.get('tran') or '').strip()
    if tran:
        return tran
    # 回退 web_trans（词典摘要翻译）
    try:
        lines = data['web_trans']['web-translation'][0]['trans'][0]['summary']['line']
        raw = ''.join(x for x in lines if isinstance(x, str))
        return raw
    except Exception:
        return ''


def _mymemory(text: str, langpair: str) -> str | None:
    """MyMemory 免费翻译 API（显式语言对）"""
    r = requests.get(
        'https://api.mymemory.translated.net/get',
        params={'q': text, 'langpair': langpair, 'de': MYMEMORY_EMAILS[0]},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get('responseStatus') != 200:
        return None
    return str((data.get('responseData') or {}).get('translatedText') or '').strip()


def _has_cjk(s: str) -> bool:
    return any('\u4e00' <= c <= '\u9fff' for c in s)


def translate_to_chinese(text: str, max_chars: int = 500) -> str:
    """免费翻译成中文：有道优先，MyMemory 兜底；失败返回原文"""
    if not text:
        return ''
    text = text[:max_chars]
    if _has_cjk(text[:20]):
        return text

    # 有道（en 自动检测）
    try:
        r = _youdao(text)
        if r and _has_cjk(r):
            return r
    except Exception as e:
        logger.warning(f'youdao: {e}')

    # MyMemory en→zh-CN
    for langpair in ['en|zh-CN', 'ja|zh-CN']:
        try:
            r = _mymemory(text, langpair)
            if r and _has_cjk(r):
                return r
        except Exception as e:
            logger.warning(f'mymemory {langpair}: {e}')
    return text


if __name__ == '__main__':
    tests = [
        'Kidnapped by a cute yandere catgirl, your only way out is to persuade her to let you leave!',
        '幼い頃から一緒に育った幼馴染との甘くてちょっとエッチな日常を描いた恋愛アドベンチャーゲーム',
        '春节快乐',
    ]
    for t in tests:
        print(f'IN: {t[:50]}')
        print(f'OUT: {translate_to_chinese(t)}')
        print()
