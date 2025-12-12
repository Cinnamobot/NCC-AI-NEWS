import datetime as dt
import json
import requests
import xml.etree.ElementTree as ET
from google import genai
from google.genai import types
import os
from dotenv import load_dotenv
import time
load_dotenv()

#ニュース取得
def get_topics(rss_url):
    topics = []
    res = requests.get(rss_url)
    root = ET.fromstring(res.text)
    for item in root[0].findall('item'):
        title = '' if item.find('title') is None else item.find('title').text
        link = '' if item.find('link') is None else item.find('link').text
        description = '' if item.find('description') is None else item.find('description').text
        pub_date = '' if item.find('pubDate') is None else item.find('pubDate').text
        if '+' in pub_date:
            pub_date = dt.datetime.strptime(pub_date, '%a, %d %b %Y %H:%M:%S %z')
        else:
            pub_date = dt.datetime.strptime(pub_date, '%a, %d %b %Y %H:%M:%S %Z')
        topic = {
            'title': title,
            'link': link,
            'description': description,
            'pub_date': pub_date.isoformat(),
        }
        topics.append(topic)
    return topics

#プロンプト送受信
def chat(request_prompt):
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

#リクエスト作成
def generate_request_prompt(prompt, content, tmp, p):
    request_prompt = {
        'context': prompt,
        'maxOutputTokens': 1024,
        'messages': [
            {
                'author': 'user',
                'content': content,
            }
        ],
        'temperature': tmp,
        'topP': p,
    }
    return request_prompt

#タグ付け（単一ニュース用 - 後方互換性のため残す）
def tag_topic(content):
    system_prompt = """
        # 命令
        入力されるニュース記事の文章に関連するタグを、出力形式に合わせて出力する。
        # 制約条件
        出力にデータ以外の情報は含めない。
        # 出力形式
        ["政治", "経済"]
    """
    request_prompt = generate_request_prompt(system_prompt, content, 0, 1)
    chat_res = chat(request_prompt)
    
    # APIエラー時のハンドリング
    if chat_res is None:
        print("APIからの応答がありませんでした。デフォルトタグを返します。")
        return ["未分類"]
    
    res_str = chat_res['candidates'][0]['text']
    
    # マークダウンのコードブロックを除去
    res_str = res_str.strip()
    if res_str.startswith('```json'):
        res_str = res_str[7:]
    if res_str.startswith('```'):
        res_str = res_str[3:]
    if res_str.endswith('```'):
        res_str = res_str[:-3]
    res_str = res_str.strip()
    
    try:
        res_dict = json.loads(res_str)
        return res_dict
    except json.JSONDecodeError as e:
        print(f"JSONパースエラー: {e}")
        print(f"受信した文字列: {repr(res_str)}")
        return ["未分類"]

#バッチタグ付け（複数ニュースをまとめて処理）
def tag_topics_batch(topics):
    """
    複数のニュースをまとめて1回のAPI呼び出しでタグ付けする。
    
    Args:
        topics: ニュース記事のリスト（各要素に'title'と'description'が必要）
    
    Returns:
        タグのリストのリスト（各ニュースに対応）
    """
    # ニュース一覧を番号付きで作成
    news_list = []
    for i, topic in enumerate(topics):
        content = topic['title'] + ' ' + (topic['description'] or '')
        news_list.append(f"{i+1}. {content}")
    
    news_text = "\n".join(news_list)
    
    system_prompt = """
        # 命令
        入力される複数のニュース記事それぞれに関連するタグを生成し、JSON形式で出力する。
        
        # 制約条件
        - 出力にデータ以外の情報は含めない
        - 入力されたニュースの番号と同じインデックスで出力すること
        - 各ニュースには最低1つのタグを付けること
        
        # 出力形式
        入力が3件の場合の例:
        [
            ["政治", "外交"],
            ["経済", "株式"],
            ["スポーツ", "野球"]
        ]
    """
    
    request_prompt = generate_request_prompt(system_prompt, news_text, 0, 1)
    # バッチ処理用にトークン数を増やす
    request_prompt['maxOutputTokens'] = 4096
    
    chat_res = chat(request_prompt)
    
    # APIエラー時のハンドリング
    if chat_res is None:
        print("APIからの応答がありませんでした。デフォルトタグを返します。")
        return [["未分類"] for _ in topics]
    
    res_str = chat_res['candidates'][0]['text']
    
    # マークダウンのコードブロックを除去
    res_str = res_str.strip()
    if res_str.startswith('```json'):
        res_str = res_str[7:]
    if res_str.startswith('```'):
        res_str = res_str[3:]
    if res_str.endswith('```'):
        res_str = res_str[:-3]
    res_str = res_str.strip()
    
    try:
        res_list = json.loads(res_str)
        # 結果の数がニュースの数と一致することを確認
        if len(res_list) != len(topics):
            print(f"警告: タグの数({len(res_list)})がニュースの数({len(topics)})と一致しません")
            # 不足分を補完
            while len(res_list) < len(topics):
                res_list.append(["未分類"])
        return res_list
    except json.JSONDecodeError as e:
        print(f"JSONパースエラー: {e}")
        print(f"受信した文字列: {repr(res_str)}")
        return [["未分類"] for _ in topics]

#実行
news_links = [
    'https://news.yahoo.co.jp/rss/topics/top-picks.xml',
    'https://www.nhk.or.jp/rss/news/cat0.xml',
    'https://biz-journal.jp/index.xml'
]
all_topics = []
for news_link in news_links:
    topics = get_topics(news_link)[:4]
    all_topics += topics

print(f"取得したニュース数: {len(all_topics)}")
print("バッチタグ付け処理を開始...")

# バッチでタグを生成
tags_list = tag_topics_batch(all_topics)

# タグを各ニュースに適用
for i, topic in enumerate(all_topics):
    topic['tags'] = tags_list[i]
    print(f"{i+1}/{len(all_topics)}: {topic['title'][:30]}... -> {topic['tags']}")

print("タグ付け完了!")

with open('all_topics.json', 'w', encoding='utf-8') as f:
    json.dump(all_topics, f, indent=4, ensure_ascii=False)