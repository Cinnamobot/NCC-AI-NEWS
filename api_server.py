"""
NCC-AI-NEWS API Server
差分タグ付け、タグ正規化、リアルタイムニュース取得機能を提供
"""
import datetime as dt
import json
import os
from pathlib import Path

import requests
import xml.etree.ElementTree as ET
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="NCC AI NEWS API")

# CORS設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 開発用: 全てのオリジンを許可
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静的ファイル配信（スタイルシートなど）
STATIC_DIR = Path(__file__).parent


@app.get("/")
async def serve_frontend():
    """フロントエンドのHTMLを返す"""
    return FileResponse(STATIC_DIR / "index.html")


# 静的ファイル配信（CSSなど）- ルートエンドポイントより後にマウント
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

DATA_FILE = Path(__file__).parent / "all_topics.json"

# RSSフィードURL
NEWS_LINKS = [
    'https://news.yahoo.co.jp/rss/topics/top-picks.xml',
    'https://www.nhk.or.jp/rss/news/cat0.xml',
    'https://biz-journal.jp/index.xml'
]


def load_existing_topics() -> list:
    """既存のニュースデータを読み込む"""
    if DATA_FILE.exists():
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def save_topics(topics: list):
    """ニュースデータを保存"""
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(topics, f, indent=4, ensure_ascii=False)


def get_topics_from_rss(rss_url: str) -> list:
    """RSSフィードからニュースを取得"""
    topics = []
    try:
        res = requests.get(rss_url, timeout=10)
        root = ET.fromstring(res.text)
        for item in root[0].findall('item'):
            title = '' if item.find('title') is None else item.find('title').text
            link = '' if item.find('link') is None else item.find('link').text
            description = '' if item.find('description') is None else item.find('description').text
            pub_date = '' if item.find('pubDate') is None else item.find('pubDate').text
            
            if pub_date:
                try:
                    if '+' in pub_date:
                        pub_date = dt.datetime.strptime(pub_date, '%a, %d %b %Y %H:%M:%S %z')
                    else:
                        pub_date = dt.datetime.strptime(pub_date, '%a, %d %b %Y %H:%M:%S %Z')
                    pub_date = pub_date.isoformat()
                except:
                    pub_date = dt.datetime.now().isoformat()
            
            topic = {
                'title': title,
                'link': link,
                'description': description or '',
                'pub_date': pub_date,
            }
            topics.append(topic)
    except Exception as e:
        print(f"RSS取得エラー ({rss_url}): {e}")
    return topics


def chat(request_prompt: dict) -> dict | None:
    """Gemini APIにリクエストを送信"""
    client = genai.Client(api_key=os.getenv('GEMINI_TOKEN'))
    content_string = request_prompt['messages'][0]['content']
    config = types.GenerateContentConfig(
        system_instruction=request_prompt['context'],
        max_output_tokens=request_prompt['maxOutputTokens'],
        temperature=request_prompt['temperature'],
        top_p=request_prompt['topP'],
    )
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash-lite',
            contents=content_string,
            config=config,
        )
        return {'candidates': [{'text': response.text}]}
    except Exception as e:
        print(f"API呼び出し中にエラーが発生しました: {e}")
        return None


def generate_request_prompt(prompt: str, content: str, tmp: float, p: float) -> dict:
    """リクエストプロンプトを生成"""
    return {
        'context': prompt,
        'maxOutputTokens': 4096,
        'messages': [{'author': 'user', 'content': content}],
        'temperature': tmp,
        'topP': p,
    }


def parse_json_response(res_str: str) -> list | None:
    """AIレスポンスからJSONをパース"""
    res_str = res_str.strip()
    if res_str.startswith('```json'):
        res_str = res_str[7:]
    if res_str.startswith('```'):
        res_str = res_str[3:]
    if res_str.endswith('```'):
        res_str = res_str[:-3]
    res_str = res_str.strip()
    
    try:
        return json.loads(res_str)
    except json.JSONDecodeError as e:
        print(f"JSONパースエラー: {e}")
        print(f"受信した文字列: {repr(res_str)}")
        return None


def tag_topics_batch_with_normalization(topics: list, existing_tags: list) -> list:
    """
    複数のニュースをまとめてタグ付け（既存タグで正規化）
    
    Args:
        topics: 新規ニュース記事のリスト
        existing_tags: 既存のタグリスト（正規化に使用）
    
    Returns:
        タグのリストのリスト
    """
    if not topics:
        return []
    
    # ニュース一覧を番号付きで作成
    news_list = []
    for i, topic in enumerate(topics):
        content = topic['title'] + ' ' + (topic['description'] or '')
        news_list.append(f"{i+1}. {content}")
    
    news_text = "\n".join(news_list)
    
    # 既存タグリストをプロンプトに含める
    existing_tags_str = json.dumps(existing_tags, ensure_ascii=False) if existing_tags else "[]"
    
    system_prompt = f"""
        # 命令
        入力される複数のニュース記事それぞれに関連するタグを生成し、JSON形式で出力する。
        
        # 制約条件
        - 出力にデータ以外の情報は含めない
        - 入力されたニュースの番号と同じインデックスで出力すること
        - 各ニュースには最低1つのタグを付けること
        - **重要**: 以下の既存タグと同義のタグは、既存タグの表記に統一すること
          例: 「ベースボール」→「野球」、「テック」→「テクノロジー」
        
        # 既存タグリスト
        {existing_tags_str}
        
        # 出力形式
        入力が3件の場合の例:
        [
            ["政治", "外交"],
            ["経済", "株式"],
            ["スポーツ", "野球"]
        ]
    """
    
    request_prompt = generate_request_prompt(system_prompt, news_text, 0, 1)
    chat_res = chat(request_prompt)
    
    if chat_res is None:
        print("APIからの応答がありませんでした。デフォルトタグを返します。")
        return [["未分類"] for _ in topics]
    
    res_str = chat_res['candidates'][0]['text']
    res_list = parse_json_response(res_str)
    
    if res_list is None:
        return [["未分類"] for _ in topics]
    
    # 結果の数がニュースの数と一致することを確認
    if len(res_list) != len(topics):
        print(f"警告: タグの数({len(res_list)})がニュースの数({len(topics)})と一致しません")
        while len(res_list) < len(topics):
            res_list.append(["未分類"])
    
    return res_list


def get_all_existing_tags(topics: list) -> list:
    """既存ニュースから全タグを抽出"""
    tags = set()
    for topic in topics:
        if 'tags' in topic and topic['tags']:
            for tag in topic['tags']:
                tags.add(tag)
    return list(tags)


def find_new_topics(fetched_topics: list, existing_topics: list) -> list:
    """新規ニュースを検出（リンクで比較）"""
    existing_links = {topic['link'] for topic in existing_topics}
    return [topic for topic in fetched_topics if topic['link'] not in existing_links]


@app.get("/api/news")
async def get_news(tags: str = Query(None, description="カンマ区切りのタグでフィルタ")):
    """
    最新ニュースを取得
    - 差分のみタグ付けを実行
    - 既存タグで正規化
    """
    # 既存データ読み込み
    existing_topics = load_existing_topics()
    existing_tags = get_all_existing_tags(existing_topics)
    existing_links = {topic['link'] for topic in existing_topics}
    
    # RSSから最新ニュース取得
    all_fetched = []
    for rss_url in NEWS_LINKS:
        topics = get_topics_from_rss(rss_url)[:4]  # 各サイト4件まで
        all_fetched.extend(topics)
    
    # 新規ニュースを検出
    new_topics = find_new_topics(all_fetched, existing_topics)
    
    if new_topics:
        print(f"新規ニュース {len(new_topics)} 件を検出")
        
        # 新規ニュースにタグ付け（既存タグで正規化）
        new_tags = tag_topics_batch_with_normalization(new_topics, existing_tags)
        
        for i, topic in enumerate(new_topics):
            topic['tags'] = new_tags[i]
        
        # 既存データと結合（新規を先頭に）
        all_topics = new_topics + existing_topics
        
        # 保存
        save_topics(all_topics)
    else:
        print("新規ニュースなし")
        all_topics = existing_topics
    
    # タグでフィルタリング
    if tags:
        filter_tags = set(tags.split(','))
        all_topics = [
            topic for topic in all_topics
            if topic.get('tags') and any(tag in filter_tags for tag in topic['tags'])
        ]
    
    return {
        "count": len(all_topics),
        "news": all_topics,
        "new_count": len(new_topics) if new_topics else 0
    }


@app.get("/api/tags")
async def get_tags():
    """利用可能なタグ一覧を取得"""
    existing_topics = load_existing_topics()
    
    # タグと出現回数を集計
    tag_counts = {}
    for topic in existing_topics:
        if topic.get('tags'):
            for tag in topic['tags']:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
    
    # 出現回数順にソート
    sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
    
    return {
        "tags": [{"name": tag, "count": count} for tag, count in sorted_tags]
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
