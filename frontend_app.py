"""
Strands Agent Graph - Streamlit Frontend
AWS AgentCore Runtime経由でAgent Graphを実行
"""

import streamlit as st
import boto3
from botocore.config import Config
import json
import os
from datetime import datetime
from typing import Dict, Any, Optional
import logging
from dotenv import load_dotenv

# 環境変数をロード（オプション：AGENT_RUNTIME_ARNなど）
load_dotenv()

# ログ設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ページ設定
st.set_page_config(
    page_title="Strands Agent Graph",
    page_icon="🤖",
    layout="wide"
)

# セッション状態の初期化
if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    import uuid
    st.session_state.session_id = str(uuid.uuid4())

# タイムアウト設定を含むboto3設定
boto_config = Config(
    read_timeout=600,  # 読み取りタイムアウトを10分に設定
    connect_timeout=120,  # 接続タイムアウトを2分に設定
    retries={
        'max_attempts': 3,
        'mode': 'standard'
    }
)

# Bedrock AgentCoreクライアントを初期化（タイムアウト設定付き）
try:
    agent_core_client = boto3.client('bedrock-agentcore', config=boto_config)
except Exception as e:
    logger.error(f"AgentCore クライアントの初期化に失敗: {e}")
    agent_core_client = None


def process_agent_response(agent_response):
    """AgentCore Runtimeからのレスポンスを処理（構造化レスポンス対応）"""
    try:
        response_data = agent_response["response"].read()
        
        # レスポンスデータをログ出力（デバッグ用）
        logger.info(f"レスポンスデータ長: {len(response_data)} bytes")
        
        # バイトデータを文字列に変換
        if isinstance(response_data, bytes):
            response_str = response_data.decode("utf-8")
        else:
            response_str = str(response_data)
        
        # 空レスポンスチェック
        if not response_str:
            return {"type": "empty", "message": "レスポンスが空でした"}
        
        logger.debug(f"レスポンス内容の先頭100文字: {response_str[:100]}")
        
        # 複数のJSON解析戦略を試行
        
        # 戦略1: 直接JSONとしてパース（構造化レスポンスの場合）
        try:
            data = json.loads(response_str)
            
            # エラーレスポンスの処理
            if isinstance(data, dict) and "error" in data:
                return {
                    "type": "error",
                    "message": data["error"]
                }
            
            # 構造化レスポンスの処理
            if isinstance(data, dict) and "agents" in data:
                logger.info("構造化レスポンスを検出しました")
                return {
                    "type": "structured",
                    "data": data
                }
            
            # 通常のJSONレスポンス
            if isinstance(data, dict) or isinstance(data, list):
                return {
                    "type": "structured",
                    "data": data
                }
            
            # テキストレスポンス
            if isinstance(data, str):
                return {
                    "type": "text",
                    "message": data
                }
                
        except json.JSONDecodeError:
            logger.debug("直接JSONパースに失敗、ストリーミング形式を試行")
        
        # 戦略2: 改行区切りのストリーミングレスポンスを処理
        try:
            for line in response_str.split("\n"):
                line = line.strip()
                if not line:
                    continue
                    
                # "data: "プレフィックスを除去
                if line.startswith("data: "):
                    line = line[6:]
                
                # JSONとしてパース
                try:
                    data = json.loads(line)
                    
                    # エラーレスポンスの処理
                    if isinstance(data, dict) and "error" in data:
                        return {
                            "type": "error",
                            "message": data["error"]
                        }
                    
                    # 構造化レスポンスの処理
                    if isinstance(data, dict) and "agents" in data:
                        logger.info("ストリーミング形式から構造化レスポンスを検出")
                        return {
                            "type": "structured",
                            "data": data
                        }
                    
                    # 通常のテキストレスポンス
                    if isinstance(data, str):
                        return {
                            "type": "text",
                            "message": data
                        }
                    
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            logger.debug(f"ストリーミング形式の処理中にエラー: {e}")
        
        # 戦略3: プレーンテキストとして扱う
        return {
            "type": "text",
            "message": response_str
        }
            
    except Exception as e:
        logger.error(f"レスポンス処理エラー: {e}")
        return {
            "type": "error",
            "message": f"レスポンス処理中にエラーが発生しました: {str(e)}"
        }


def format_structured_response(data: Dict[str, Any]) -> str:
    """構造化レスポンスをMarkdown形式にフォーマット"""
    lines = []
    
    # ステータス情報をコンパクトに表示
    status_icon = "✅" if data.get("status") == "completed" else "❌"
    status_text = "処理完了" if data.get("status") == "completed" else "処理失敗"
    lines.append(f"### {status_icon} {status_text}")
    
    # 実行統計をインラインで表示
    stats_parts = []
    if data.get("total_execution_time_ms"):
        time_sec = data["total_execution_time_ms"] / 1000
        stats_parts.append(f"⏱️ {time_sec:.2f}秒")
    
    if data.get("total_tokens"):
        stats_parts.append(f"🎯 {data['total_tokens']:,}トークン")
    
    if data.get("mcp_tools_used"):
        stats_parts.append("🔧 MCPツール使用")
    
    if stats_parts:
        lines.append(f"*{' | '.join(stats_parts)}*")
    
    lines.append("\n---\n")
    
    # 各エージェントの結果を整形表示
    if data.get("agents"):
        for i, agent in enumerate(data["agents"]):
            agent_name = agent.get("name", "Unknown")
            
            # エージェント名を見やすく変換
            display_name = {
                "slack_agent": "Slackエージェント",
                "tavily_agent": "Tavilyエージェント",
                "block_agent": "ブロックエージェント"
            }.get(agent_name, agent_name)
            
            # エージェントヘッダー
            if i > 0:
                lines.append("")  # エージェント間にスペースを追加
            
            # エージェント名と実行時間を同じ行に
            agent_header = f"#### 📦 **{display_name}**"
            if agent.get("execution_time_ms"):
                time_sec = agent["execution_time_ms"] / 1000
                agent_header += f" *(実行時間: {time_sec:.2f}秒)*"
            lines.append(agent_header)
            
            # メッセージを整形
            has_content = False
            for msg in agent.get("messages", []):
                if msg["type"] == "text":
                    content = msg['content'].strip()
                    if content:
                        has_content = True
                        # インデントを追加して見やすく
                        lines.append("")
                        # 複数行のテキストを適切に処理
                        content_lines = content.split('\n')
                        for line in content_lines:
                            if line.strip():
                                lines.append(f"> {line}")
                        
                elif msg["type"] == "json":
                    has_content = True
                    lines.append("")
                    lines.append("```json")
                    if isinstance(msg['content'], list):
                        for item in msg['content']:
                            lines.append(json.dumps(item, ensure_ascii=False, indent=2))
                    else:
                        lines.append(json.dumps(msg['content'], ensure_ascii=False, indent=2))
                    lines.append("```")
            
            # メッセージがない場合
            if not has_content:
                if agent.get("status") == "skipped":
                    lines.append("> *（スキップされました）*")
                else:
                    lines.append("> *（出力なし）*")
    
    # フルテキストがある場合（フォールバック）
    elif data.get("full_text"):
        lines.append("### 📝 処理結果\n")
        # フルテキストを整形
        full_text_lines = data["full_text"].split('\n')
        for line in full_text_lines:
            if line.strip():
                lines.append(line)
    
    # メタデータがある場合は最後に追加
    if data.get("metadata"):
        metadata = data["metadata"]
        if any([metadata.get("total_nodes"), metadata.get("completed_nodes")]):
            lines.append("\n---\n")
            lines.append("##### 📊 実行詳細")
            details = []
            if metadata.get("total_nodes"):
                details.append(f"総ノード数: {metadata['total_nodes']}")
            if metadata.get("completed_nodes"):
                details.append(f"完了: {metadata['completed_nodes']}")
            if metadata.get("failed_nodes", 0) > 0:
                details.append(f"失敗: {metadata['failed_nodes']}")
            lines.append(f"*{' | '.join(details)}*")
    
    return "\n".join(lines)


def render_sidebar():
    """サイドバーを表示"""
    with st.sidebar:
        st.title("⚙️ 設定")
        
        # AgentCore Runtime設定
        st.subheader("📊 AgentCore Runtime設定")
        
        # AGENT_RUNTIME_ARN
        agent_runtime_arn = os.getenv("AGENT_RUNTIME_ARN", "")
        if agent_runtime_arn:
            st.success(f"✅ AGENT_RUNTIME_ARN: 設定済み")
            with st.expander("ARN詳細"):
                st.code(agent_runtime_arn)
        else:
            st.warning("⚠️ AGENT_RUNTIME_ARN: 未設定")
            arn_input = st.text_input(
                "Agent Runtime ARN",
                placeholder="arn:aws:bedrock:region:account:agent-runtime/xxxxx",
                help="AgentCore RuntimeのARNを入力してください"
            )
            if arn_input:
                os.environ["AGENT_RUNTIME_ARN"] = arn_input
                st.rerun()
        
        # M2M認証設定
        st.divider()
        st.subheader("🔐 M2M認証設定")
        
        # Runtime User ID設定
        current_user_id = os.getenv("RUNTIME_USER_ID", "m2m-user-001")
        user_id_input = st.text_input(
            "Runtime User ID",
            value=current_user_id,
            help="M2M認証用のユーザーID（デフォルト: m2m-user-001）"
        )
        if user_id_input != current_user_id:
            os.environ["RUNTIME_USER_ID"] = user_id_input
            st.success(f"✅ User ID更新: {user_id_input}")
        
        with st.expander("M2M認証について"):
            st.markdown("""
            **M2M (Machine-to-Machine) 認証**
            - AgentCore IdentityがWorkload Access Tokenを取得
            - Runtime User IDで認証ユーザーを識別
            - デフォルトは `m2m-user-001`
            """)
        
        # AWS設定
        st.divider()
        st.subheader("🔧 AWS設定")
        
        if agent_core_client:
            st.success("✅ AWS接続: 正常")
            # リージョン情報を表示
            try:
                region = agent_core_client.meta.region_name
                st.info(f"リージョン: {region}")
            except:
                pass
        else:
            st.error("❌ AWS接続: 失敗")
            st.info("AWS認証情報を確認してください")
        
        # セッション情報
        st.divider()
        st.subheader("📝 セッション情報")
        st.info(f"セッションID:\n`{st.session_state.session_id}`")
        
        # 会話履歴のクリア
        if st.button("🗑️ 会話履歴をクリア", type="secondary", use_container_width=True):
            st.session_state.messages = []
            st.rerun()


def render_chat_interface():
    """チャットインターフェースを表示"""
    
    # メインコンテンツエリア
    st.title("🤖 Strands Agent Graph")
    st.markdown("""
    **SlackとTavilyを連携したマルチエージェントシステム**
    
    このアプリは以下の処理を自動実行します：
    1. SlackAgentが「test-strands-agents」チャンネルからURLを取得
    2. TavilyAgentがURLの内容を抽出・要約
    """)
    
    # 設定チェック
    if not agent_core_client:
        st.error("❌ AWS AgentCoreクライアントが初期化されていません")
        st.info("AWS認証情報を確認してください")
        return
    
    if not os.getenv("AGENT_RUNTIME_ARN"):
        st.warning("""
        ⚠️ **Agent Runtime ARNが設定されていません**
        
        サイドバーでAgent Runtime ARNを設定してください。
        """)
        return
    
    # チャット履歴を表示
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    
    # チャット入力
    if prompt := st.chat_input("メッセージを入力してください（例：SlackのURLを要約して）"):
        # ユーザーメッセージを追加
        st.session_state.messages.append({
            "role": "user",
            "content": prompt
        })
        
        # ユーザーメッセージを表示
        with st.chat_message("user"):
            st.markdown(prompt)
        
        # アシスタントの応答を表示
        with st.chat_message("assistant"):
            response_container = st.container()
            
            try:
                # ペイロードを作成
                payload = json.dumps({
                    "input": {
                        "prompt": prompt,
                        "session_id": st.session_state.session_id
                    }
                }).encode()
                
                # AgentCore Runtimeを呼び出し（タイムアウトを延長）
                with st.spinner("🔄 Agent Graphを実行中...（処理に時間がかかる場合があります）"):
                    logger.info(f"Agent Runtime呼び出し: ARN={os.getenv('AGENT_RUNTIME_ARN')}")
                    logger.info(f"セッションID: {st.session_state.session_id}")
                    logger.info(f"タイムアウト設定: 読み取り=600秒（10分）, 接続=120秒（2分）")
                    
                    # runtimeUserIdを設定（M2M認証用）
                    runtime_user_id = os.getenv("RUNTIME_USER_ID", "m2m-user-001")
                    logger.info(f"Runtime User ID: {runtime_user_id}")
                    
                    agent_response = agent_core_client.invoke_agent_runtime(
                        agentRuntimeArn=os.getenv("AGENT_RUNTIME_ARN"),
                        runtimeSessionId=st.session_state.session_id,
                        payload=payload,
                        qualifier="DEFAULT",
                        runtimeUserId=runtime_user_id  # ユーザーIDをヘッダーに設定
                    )
                    
                    # レスポンスを処理（構造化レスポンス対応）
                    response_result = process_agent_response(agent_response)
                    
                    # レスポンスタイプに応じて表示
                    if response_result["type"] == "error":
                        response_container.error(f"❌ {response_result['message']}")
                        display_content = response_result['message']
                    
                    elif response_result["type"] == "empty":
                        response_container.warning(response_result['message'])
                        display_content = response_result['message']
                    
                    elif response_result["type"] == "structured":
                        # 構造化レスポンスをフォーマット
                        formatted_response = format_structured_response(response_result['data'])
                        response_container.markdown(formatted_response)
                        display_content = formatted_response
                        
                        # デバッグ情報を展開可能セクションに表示
                        with response_container.expander("🔍 詳細情報"):
                            st.json(response_result['data'])
                    
                    elif response_result["type"] == "text":
                        # テキストレスポンスがJSON形式の場合を検出
                        message = response_result['message']
                        
                        # JSONとして解析を試みる
                        try:
                            # 単純な文字列の場合、JSON構造化レスポンスの可能性をチェック
                            if message.strip().startswith('{') and message.strip().endswith('}'):
                                potential_json = json.loads(message)
                                
                                # 構造化レスポンスのキーを持っているか確認
                                if isinstance(potential_json, dict) and "agents" in potential_json and "status" in potential_json:
                                    logger.info("テキストレスポンス内に構造化JSONを検出")
                                    # 構造化レスポンスとして処理
                                    formatted_response = format_structured_response(potential_json)
                                    response_container.markdown(formatted_response)
                                    display_content = formatted_response
                                    
                                    # デバッグ情報を展開可能セクションに表示
                                    with response_container.expander("🔍 詳細情報"):
                                        st.json(potential_json)
                                else:
                                    # 通常のテキストとして表示
                                    response_container.markdown(message)
                                    display_content = message
                            else:
                                # 通常のテキストとして表示
                                response_container.markdown(message)
                                display_content = message
                        except (json.JSONDecodeError, Exception) as e:
                            # JSON解析に失敗した場合は通常のテキストとして表示
                            response_container.markdown(message)
                            display_content = message
                    
                    else:
                        response_container.warning("不明なレスポンスタイプ")
                        display_content = str(response_result)
                    
                    # アシスタントメッセージを履歴に追加
                    if display_content:
                        st.session_state.messages.append({
                            "role": "assistant",
                            "content": display_content
                        })
                    
            except Exception as e:
                error_msg = f"❌ エラーが発生しました: {str(e)}"
                response_container.error(error_msg)
                logger.error(f"実行エラー: {e}")
                
                # エラーメッセージも履歴に追加
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": error_msg
                })


def main():
    """メイン関数"""
    # サイドバーを表示
    render_sidebar()
    
    # チャットインターフェースを表示
    render_chat_interface()


if __name__ == "__main__":
    main()
