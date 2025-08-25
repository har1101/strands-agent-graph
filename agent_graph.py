from strands import Agent
from strands.tools.mcp import MCPClient
from strands.multiagent import GraphBuilder
from strands.multiagent.graph import GraphState
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from typing import Any, Dict, List, Optional
import logging
import json
from boto3.session import Session
import os

# MCPクライアント用のインポート
from mcp.client.streamable_http import streamablehttp_client

# AgentCore Identityからアクセストークンを取得する
from bedrock_agentcore.identity.auth import requires_access_token

logger = logging.getLogger("agent_graph")
logger.setLevel(logging.DEBUG)
logging.basicConfig(
    format="%(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler()]
)

boto_session = Session()
region = boto_session.region_name

class ResearchAgent:
    """
    Cognito M2M認証を使用したAgentCore Identityを利用するエージェント。
    
    必要な環境変数：
    - GATEWAY_URL: Slackツールを提供するGatewayのエンドポイント
    - COGNITO_SCOPE: Cognito OAuth2のスコープ
    - WORKLOAD_NAME: （オプション）workload名、デフォルトは"slack-gateway-agent"
    - USER_ID: (オプション)user-idを設定する、デフォルトは"m2m-user-001"
    """

    def __init__(self):
        self.gateway_url = os.environ.get("GATEWAY_URL", "https://slack-gateway-uzumouvte3.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
        self.cognito_scope = os.environ.get("COGNITO_SCOPE", "slack-gateway/genesis-gateway:invoke")
        self.workload_name = os.environ.get("WORKLOAD_NAME", "slack-gateway-agent")
        self.user_id = os.environ.get("USER_ID", "m2m-user-001")
        self.region = region
        
        # 環境変数の検証
        if not self.gateway_url:
            raise ValueError("GATEWAY_URL環境変数が必要です")
        if not self.cognito_scope:
            raise ValueError("COGNITO_SCOPE環境変数が必要です")
            
        logger.info(f"Gateway URL: {self.gateway_url}")
        logger.info(f"Cognito scope: {self.cognito_scope}")
        logger.info(f"Workload name: {self.workload_name}")
        logger.info(f"User ID: {self.user_id}")
        logger.info(f"AWS Region: {self.region}")

    async def get_access_token(self) -> str:
        """AgentCore Identityを使用してアクセストークンを取得する。
        
        Runtime環境では、runtimeUserIdはInvokeAgentRuntime API呼び出し時に
        システム側が設定し、Runtimeがエージェントに渡します。
        
        Returns:
            str: 認証されたAPIコール用のアクセストークン
        """
        
        # @requires_access_tokenデコレータ付きのラッパー関数を作成
        # Runtime環境では、デコレータが内部で_get_workload_access_tokenを呼び出し、
        # workload access tokenを自動的に取得する
        @requires_access_token(
            provider_name="agentcore-identity-for-gateway",
            scopes=[self.cognito_scope],
            auth_flow="M2M",
            force_authentication=False,
        )
        async def _get_token(*, access_token: str) -> str:
            """
            AgentCore Identityからアクセストークンを受け取る内部関数。
            
            デコレータが内部で以下を処理：
            1. _get_workload_access_tokenを呼び出してworkload access tokenを取得
                - workload_name: Runtime環境から取得
                - user_id: InvokeAgentRuntimeのruntimeUserIdヘッダーから取得
            2. workload access tokenを使用してOAuth tokenを取得
            3. access_tokenパラメータとして注入
            
            Args:
                access_token: OAuthアクセストークン（デコレータによって注入）
                
            Returns:
                str: APIコールで使用するアクセストークン
            """
            logger.info("✅ AgentCore Identity経由でアクセストークンの取得に成功")
            logger.info(f"   Workload name: {self.workload_name}")
            logger.info(f"   トークンプレフィックス: {access_token[:20]}...")
            logger.info(f"   トークン長: {len(access_token)} 文字")
            return access_token
        
        # デコレータ付き関数を呼び出してトークンを取得
        return await _get_token()
    
    async def create_mcp_client_and_tools(self) -> tuple[MCPClient, list]:
        """
        トークン取得 → MCPクライアントとツールのリストを返す。
        
        MCPクライアントはwithコンテキスト内で使用する必要があるため、
        クライアントインスタンスとツールのリストを返します。

        Returns:
            tuple[MCPClient, list]: MCPクライアントインスタンスと利用可能なツールのリスト
        """

        # ステップ1: AgentCore Identityを使用してアクセストークンを取得
        logger.info("ステップ1: AgentCore Identity経由でアクセストークンを取得中...")
        logger.info(f"Runtimeが自動的にruntimeUserIdを渡します")
        
        access_token = await self.get_access_token()
        
        # ステップ2: 認証されたMCPクライアントを作成
        logger.info("ステップ2: 認証されたMCPクライアントを作成中...")

        def create_streamable_http_transport():
            """
            Bearerトークン認証を使用したストリーミング可能なHTTPトランスポートを作成。
            
            このトランスポートは、MCPクライアントがGatewayへの認証された
            リクエストを行うために使用されます。
            """
            logger.info(f"🔗 MCP transport作成中: {self.gateway_url}")
            logger.info(f"🔑 トークンプレフィックス: {access_token[:20]}...")
            transport = streamablehttp_client(
                self.gateway_url, 
                headers={"Authorization": f"Bearer {access_token}"}
            )
            logger.info("✅ MCP transport作成完了")
            return transport
        
        # 認証されたトランスポートでMCPクライアントを作成
        mcp_client = MCPClient(create_streamable_http_transport)
        
        return mcp_client
    
    def get_full_tools_list(self, client):
        """
        ページネーションをサポートしてすべての利用可能なツールをリスト。
        
        Gatewayはページネーションされたレスポンスでツールを返す可能性があるため、
        完全なリストを取得するためにページネーションを処理する必要があります。
        
        Args:
            client: MCPクライアントインスタンス
            
        Returns:
            list: 利用可能なツールの完全なリスト
        """
        more_tools = True
        tools = []
        pagination_token = None
        
        while more_tools:
            tmp_tools = client.list_tools_sync(pagination_token=pagination_token)
            tools.extend(tmp_tools)
            
            if tmp_tools.pagination_token is None:
                more_tools = False
            else:
                more_tools = True 
                pagination_token = tmp_tools.pagination_token
        return tools

# AgentCoreアプリケーションを初期化
app = BedrockAgentCoreApp()

@app.entrypoint
async def invoke_agent_graph(payload: Dict[str, Any]):
    """Agent Graphのメインエントリーポイント
    
    Args:
        payload: AgentCore Runtimeから渡されるペイロード
                - prompt: ユーザーからの入力メッセージ
    
    Yields:
        AgentCore Runtime形式のストリーミングレスポンス
    """
    
    try:
        # ResearchAgentインスタンスを作成
        agent_with_identity = ResearchAgent()
    except ValueError as e:
        # 環境変数が設定されていない場合のエラー
        logger.error(f"設定エラー: {e}")
        yield {"error": f"設定エラー: {str(e)}. GATEWAY_URLとCOGNITO_SCOPE環境変数が設定されていることを確認してください。"}
        return
    except Exception as e:
        # その他の初期化エラー
        logger.error(f"初期化エラー: {e}")
        yield {"error": f"エージェントの初期化に失敗しました: {str(e)}"}
        return
    
    # プロンプトの検証とペイロード構造の処理
    user_message = ""
    
    # ペイロードが入れ子構造の場合（AgentCore Runtime経由）
    if payload and "input" in payload:
        input_data = payload["input"]
        if isinstance(input_data, dict):
            user_message = input_data.get("prompt", "")
        elif isinstance(input_data, str):
            # inputが文字列の場合はJSONとして解析
            try:
                input_json = json.loads(input_data)
                user_message = input_json.get("prompt", "")
            except:
                user_message = input_data
    # 直接promptが含まれる場合
    elif payload and "prompt" in payload:
        user_message = payload["prompt"]
    
    if not user_message:
        logger.error(f"無効なペイロード構造: {payload}")
        yield {"error": "無効なペイロード: 'prompt'フィールドが必要です"}
        return
    
    # システムプロンプトを定義
    slack_agent_system_prompt = """
    あなたはSlack統合アシスタントです。
    「test-strands-agents」というチャンネルからURLが添付されているメッセージを丸ごと取得してきてください。
    """

    tavily_agent_system_prompt = """
    あなたはTavily統合アシスタントです。
    取得したURLを元に、extractツールを用いて本文を抽出し、内容を要約してください。
    """
    
    try:
        # MCPクライアントとツールを作成
        logger.info("🚀 MCPクライアント作成を開始...")
        mcp_client = await agent_with_identity.create_mcp_client_and_tools()
        
        # MCPのwithコンテキスト内でGraph全体を実行
        logger.info("📦 MCPコンテキストを開始（セッション維持）...")
        with mcp_client:
            logger.info("✅ MCPコンテキストに入りました - セッションアクティブ")
            
            # ステップ3: 認証された接続を通じて利用可能なツールをリスト
            logger.info("ステップ3: 認証されたMCPクライアント経由で利用可能なツールをリスト中...")
            tools = agent_with_identity.get_full_tools_list(mcp_client)
            
            # MCPツールの属性名を確認してからログ出力
            try:
                tools_names = [getattr(tool, 'tool_name', getattr(tool, 'name', str(tool))) for tool in tools]
            except Exception as e:
                logger.warning(f"ツール名の取得に失敗: {e}")
                tools_names = [str(tool) for tool in tools]
            logger.info(f"利用可能なツール: {tools_names}")
            logger.info(f"📊 取得したツール数: {len(tools)}")

            if not tools:
                raise RuntimeError("Gatewayから利用可能なツールがありません")
            
            # ツールをフィルタリング
            # SlackAgent用: ツール名に「slack」が含まれるツールのみ
            slack_tools = []
            for tool in tools:
                tool_name = getattr(tool, 'tool_name', getattr(tool, 'name', str(tool)))
                if 'slack' in tool_name.lower():
                    slack_tools.append(tool)
            
            # TavilyAgent用: ツール名に「tavily」が含まれるツールのみ
            tavily_tools = []
            for tool in tools:
                tool_name = getattr(tool, 'tool_name', getattr(tool, 'name', str(tool)))
                if 'tavily' in tool_name.lower():
                    tavily_tools.append(tool)
            
            # フィルタリング結果をログ出力
            try:
                slack_tools_names = [getattr(tool, 'tool_name', getattr(tool, 'name', str(tool))) for tool in slack_tools]
                tavily_tools_names = [getattr(tool, 'tool_name', getattr(tool, 'name', str(tool))) for tool in tavily_tools]
            except Exception as e:
                logger.warning(f"フィルタ済みツール名の取得に失敗: {e}")
                slack_tools_names = [str(tool) for tool in slack_tools]
                tavily_tools_names = [str(tool) for tool in tavily_tools]
            
            logger.info(f"📊 SlackAgent用ツール ({len(slack_tools)}個): {slack_tools_names}")
            logger.info(f"📊 TavilyAgent用ツール ({len(tavily_tools)}個): {tavily_tools_names}")
            
            # 各エージェントが使用するツールの検証
            if not slack_tools:
                logger.warning("⚠️ SlackAgent用のツールが見つかりません。全ツールを使用します。")
                slack_tools = tools
            
            if not tavily_tools:
                logger.warning("⚠️ TavilyAgent用のツールが見つかりません。全ツールを使用します。")
                tavily_tools = tools
            
            # ステップ4: フィルタリング済みツールでエージェントを作成
            logger.info(f"ステップ4: Slackツールのみで 'SlackAgent' を作成中...")
            slack_agent = Agent(
                tools=slack_tools,  # Slackツールのみを使用
                model="us.anthropic.claude-sonnet-4-20250514-v1:0",
                system_prompt=slack_agent_system_prompt
            )
            logger.info(f"SlackAgent作成完了（{len(slack_tools)}個のツールを使用）")

            logger.info(f"ステップ5: Tavilyツールのみで 'TavilyAgent' を作成中...")
            tavily_agent = Agent(
                tools=tavily_tools,  # Tavilyツールのみを使用
                model="us.anthropic.claude-sonnet-4-20250514-v1:0",
                system_prompt=tavily_agent_system_prompt
            )
            logger.info(f"TavilyAgent作成完了（{len(tavily_tools)}個のツールを使用）")

            block_agent = Agent()

            # Graphを作成していく
            builder = GraphBuilder()
            
            # ノードを追加
            builder.add_node(slack_agent, "slack_agent")
            builder.add_node(tavily_agent, "tavily_agent")
            builder.add_node(block_agent, "block_agent")

            # 常にFalseを返す条件関数を定義（終了ポイントとして機能）
            def always_false_condition(state: GraphState) -> bool:
                """常にFalseを返してグラフを終了させる条件"""
                logger.info("🔚 終了条件を評価 - 常にFalseを返してグラフを終了")
                return False

            # エッジを追加
            builder.add_edge("slack_agent", "tavily_agent")
            
            # tavily_agentの後に条件付きエッジを追加（常にFalseで終了）
            # これによりtavily_agentの後でグラフが確実に終了する
            builder.add_edge("tavily_agent", "block_agent", condition=always_false_condition)

            # エントリーポイントの設定
            builder.set_entry_point("slack_agent")

            # Graphをビルドする
            graph = builder.build()

            # ユーザーメッセージはすでに取得済み
            logger.info(f"ユーザーメッセージ: {user_message}")

            # MCPコンテキスト内で処理を実行
            logger.info("🎯 MCPコンテキスト内でエージェント処理を開始...")
            
            # Graph.invoke_async()を使用して非同期実行
            # tavily_agentは出力エッジを持たないため、自動的に終了ポイントとなる
            try:
                # 非同期実行でGraphを実行
                logger.info("🚀 Graph.invoke_async()を開始...")
                graph_result = await graph.invoke_async(user_message)
                
                # 結果の処理（graph_with_tool_response_format.mdに基づく改善版）
                logger.info("🔍 Graph実行結果を処理中...")
                import json
                from strands.multiagent.base import Status

                def extract_message_content(agent_result):
                    """AgentResultからメッセージコンテンツを抽出"""
                    try:
                        message = getattr(agent_result, "message", {}) or {}
                        content = message.get("content", [])
                        texts = []
                        jsons = []
                        
                        for block in content:
                            if isinstance(block, dict):
                                if "text" in block:
                                    texts.append(block["text"])
                                if "json" in block:
                                    jsons.append(block["json"])
                                # toolResultの中も再帰的に処理
                                if "toolResult" in block:
                                    for inner in block.get("toolResult", {}).get("content", []):
                                        if isinstance(inner, dict):
                                            if "text" in inner:
                                                texts.append(inner["text"])
                                            if "json" in inner:
                                                jsons.append(inner["json"])
                        
                        return "\n".join(texts).strip(), jsons
                    except Exception as e:
                        logger.error(f"メッセージ抽出エラー: {e}")
                        return "", []

                def detect_mcp_usage(text: str) -> bool:
                    """MCPツールが使用されたかを検出"""
                    mcp_indicators = ["slack_", "tavily_", "extract", "search"]
                    return any(indicator in text.lower() for indicator in mcp_indicators)

                # 構造化されたレスポンスを作成
                structured_response = {
                    "status": "completed" if graph_result.status == Status.COMPLETED else "failed",
                    "agents": [],
                    "total_execution_time_ms": getattr(graph_result, "execution_time", 0),
                    "total_tokens": graph_result.accumulated_usage.get("totalTokens", 0) if hasattr(graph_result, "accumulated_usage") else 0,
                    "mcp_tools_used": False,
                    "full_text": "",  # フロントエンド表示用の統合テキスト
                    "metadata": {
                        "session_id": payload.get("sessionId", "unknown"),
                        "total_nodes": getattr(graph_result, "total_nodes", 0),
                        "completed_nodes": getattr(graph_result, "completed_nodes", 0),
                        "failed_nodes": getattr(graph_result, "failed_nodes", 0)
                    }
                }

                all_texts = []
                logger.info(f"📊 Graph全体ステータス: {structured_response['status']}")

                # 各ノードの結果を処理
                for node_name, node_result in graph_result.results.items():
                    node_data = {
                        "name": node_name,
                        "messages": [],
                        "execution_time_ms": getattr(node_result, "execution_time", 0),
                        "status": str(getattr(node_result, "status", "unknown")),
                        "tokens_used": node_result.accumulated_usage.get("totalTokens", 0) if hasattr(node_result, "accumulated_usage") else 0
                    }
                    
                    # NodeResult.get_agent_results() で入れ子もフラットに
                    for agent_result in node_result.get_agent_results():
                        text, jsons = extract_message_content(agent_result)
                        
                        if text:
                            node_data["messages"].append({
                                "type": "text",
                                "content": text
                            })
                            all_texts.append(f"[{node_name}] {text}")
                            
                            # MCPツール使用を検出
                            if detect_mcp_usage(text):
                                structured_response["mcp_tools_used"] = True
                        
                        if jsons:
                            node_data["messages"].append({
                                "type": "json",
                                "content": jsons
                            })
                        
                        # ログ出力
                        logger.info(
                            f"📦 Node: {node_name} | status={node_data['status']} | "
                            f"stop_reason={getattr(agent_result,'stop_reason',None)}"
                        )
                    
                    structured_response["agents"].append(node_data)

                # 全体の統合テキストを作成
                structured_response["full_text"] = "\n\n".join(all_texts) if all_texts else "レスポンスが空でした"
                
                # 結果をログ出力
                logger.info(f"✅ 最終レスポンス準備完了: {len(structured_response['full_text'])} 文字")
                logger.info(f"📊 MCPツール使用: {structured_response['mcp_tools_used']}")
                logger.info(f"⏱️ 総実行時間: {structured_response['total_execution_time_ms']}ms")
                logger.info(f"🎯 トークン使用量: {structured_response['total_tokens']}")
                
                # 構造化されたレスポンスをJSON形式で返す
                yield json.dumps(structured_response, ensure_ascii=False)
                
            except Exception as graph_error:
                logger.error(f"Graph実行中にエラーが発生: {graph_error}")
                # エラーの詳細をログ出力
                import traceback
                logger.error(f"スタックトレース: {traceback.format_exc()}")
                
                # エラーレスポンスを返す
                yield {
                    "type": "error",
                    "error": f"Graph実行エラー: {str(graph_error)}"
                }
                return

            logger.info("🎉 Graph処理完了 - MCPセッションを正常にクローズします")
                
    except RuntimeError as e:
        # create_agentからのエラー
        logger.error(f"❌ エージェント作成エラー: {e}")
        yield {"error": str(e)}
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        logger.error(f"❌ 処理中にエラーが発生: {e}")
        logger.error(f"📊 詳細なスタックトレース:\n{error_trace}")
        
        # エラーメッセージを改善
        error_msg = str(e)
        if "connection" in error_msg.lower() or "mcp" in error_msg.lower():
            yield {"error": f"MCP接続エラー: {error_msg}. MCPクライアントのセッションが切れている可能性があります。"}
        elif "tool" in error_msg.lower():
            yield {"error": f"ツール実行エラー: {error_msg}. ツールの利用権限またはパラメータを確認してください。"}
        else:
            yield {"error": f"リクエストの処理中にエラーが発生しました: {error_msg}"}

if __name__ == "__main__":
    # Slackツール連携エージェントサーバーを起動
    # デフォルトでポート8080でリッスンします
    app.run()
