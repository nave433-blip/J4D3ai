import os
import json
import webbrowser
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from rich.console import Console
from rich.panel import Panel
import keyring
from core.utils import open_url
from core.config import CONFIG_DIR

console = Console()

# Scopes for Google Auth - adding userinfo to get email for identifying other accounts
SCOPES = [
    'https://www.googleapis.com/auth/userinfo.email',
    'openid',
    'https://www.googleapis.com/auth/generative-language' # Access to Gemini
]

KEYRING_SERVICE = "jade_google_auth"
# Pre-rename service name: read for compatibility, never written.
_KEYRING_SERVICE_LEGACY = "jarvis_google_auth"
TOKEN_KEY = "google_token"

class GoogleAuth:
    @staticmethod
    def get_credentials():
        """Retrieve credentials from keychain or run flow."""
        creds = None
        try:
            token_json = keyring.get_password(KEYRING_SERVICE, TOKEN_KEY)
            if not token_json:
                token_json = keyring.get_password(_KEYRING_SERVICE_LEGACY, TOKEN_KEY)
            if token_json:
                from google.oauth2.credentials import Credentials
                creds_data = json.loads(token_json)
                creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
        except Exception as e:
            console.print(f"[dim]Note: Could not load Google creds from keychain: {e}[/dim]")

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    from google.auth.transport.requests import Request
                    creds.refresh(Request())
                except Exception:
                    creds = GoogleAuth.run_flow()
            else:
                creds = GoogleAuth.run_flow()
            
            # Save valid creds back to keychain
            if creds:
                keyring.set_password(KEYRING_SERVICE, TOKEN_KEY, creds.to_json())
        
        return creds

    @staticmethod
    def run_flow():
        """Run the OAuth2 flow to get a token."""
        from google_auth_oauthlib.flow import InstalledAppFlow
        console.print(Panel("🌐 [bold cyan]Initiating Google Login Flow[/bold cyan]\n\nA browser window will open. Please login with your Google account to link your AI services.", border_style="cyan"))
        
        # Look for client secrets at ~/.jade/google_client.json
        client_secrets_path = str(CONFIG_DIR / "google_client.json")
        if not os.path.exists(client_secrets_path):
            console.print("[yellow]⚠️ Warning: Google OAuth Client Secrets not found at ~/.jade/google_client.json[/yellow]")
            console.print("Please provide your Google Cloud Console Client ID and Secret.")
            client_id = console.input("Client ID: ").strip()
            client_secret = console.input("Client Secret: ").strip()
            
            if not client_id or not client_secret:
                console.print("[red]Aborted: Missing credentials.[/red]")
                return None
            
            secrets_data = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"]
                }
            }
            with open(client_secrets_path, "w") as f:
                json.dump(secrets_data, f)

        try:
            flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, SCOPES)
            
            # Monkeypatch webbrowser.open to use our robust open_url that prefers Chrome
            original_open = webbrowser.open
            webbrowser.open = open_url
            try:
                creds = flow.run_local_server(port=0)
            finally:
                webbrowser.open = original_open
                
            return creds
        except Exception as e:
            console.print(f"[bold red]❌ Google Login Failed:[/bold red] {e}")
            return None

    @staticmethod
    def get_user_email(creds):
        """Fetch user email to identify other accounts."""
        from googleapiclient.discovery import build
        service = build('oauth2', 'v2', credentials=creds)
        user_info = service.userinfo().get().execute()
        return user_info.get('email')

    @staticmethod
    def auto_register_flow():
        """
        Orchestrates a semi-automated registration flow across all Google-compatible AI providers.
        """
        creds = GoogleAuth.get_credentials()
        if not creds: return
        
        email = GoogleAuth.get_user_email(creds)
        console.print(Panel(f"🚀 [bold cyan]Starting AI Registration Orchestrator[/bold cyan]\n\nLogged in as: [bold green]{email}[/bold green]\n\nJADE will now open registration pages for all major AI services. \n[bold yellow]Requirement:[/bold yellow] Click 'Continue with Google' on each page, create your API key, and paste it back here.", border_style="cyan"))

        from core.services import set_api_key
        
        # Comprehensive list of Google-login compatible AI services
        target_services = {
            "openai": {"url": "https://platform.openai.com/api-keys", "display": "OpenAI"},
            "anthropic": {"url": "https://console.anthropic.com/settings/keys", "display": "Anthropic"},
            "perplexity": {"url": "https://www.perplexity.ai/settings/api", "display": "Perplexity"},
            "gemini": {"url": "https://aistudio.google.com/app/apikey", "display": "Google Gemini"},
            "mistral": {"url": "https://console.mistral.ai/api-keys/", "display": "Mistral AI"},
            "groq": {"url": "https://console.groq.com/keys", "display": "Groq"},
            "deepseek": {"url": "https://platform.deepseek.com/api_keys", "display": "DeepSeek"},
            "qwen": {"url": "https://dashscope.console.aliyun.com/apiKey", "display": "Alibaba Qwen"},
            "together": {"url": "https://api.together.xyz/settings/api-keys", "display": "Together AI"},
        }

        for name, info in target_services.items():
            console.print(f"\n[bold magenta]Step: Registering for {info['display']}...[/bold magenta]")
            console.print(f"Opening: {info['url']}")
            open_url(info['url'])
            
            console.print(f"[dim]Instruction: Sign up with {email}, generate an API key, and paste it below.[/dim]")
            key = console.input(f"Paste {info['display']} API Key (or press Enter to skip): ").strip()
            
            if key:
                res = set_api_key(name, key)
                if res.get("ok"):
                    console.print(f"[bold green]✅ {info['display']} secured in Keychain.[/bold green]")
                else:
                    console.print(f"[red]❌ Failed to secure {info['display']} key.[/red]")
            else:
                console.print(f"[yellow]⏩ Skipped {info['display']}.[/yellow]")

        console.print(Panel("🏁 [bold green]All targeted accounts processed![/bold green]\n\nYour JADE ecosystem is now fully powered and authenticated.", border_style="green"))
