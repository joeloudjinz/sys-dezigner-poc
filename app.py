# app.py
"""
The main Flet GUI application for the System Design Co-Pilot.
- Implements a glassmorphism theme.
- Provides a real-time chat interface with streaming responses.
- Features a persistent history sidebar to load past discussions.
- Uses threading to keep the UI responsive during agent processing.
- Displays errors gracefully using a non-blocking SnackBar.
"""

import flet as ft
import os
import threading
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from agent import SystemDesignAgent
from database import DatabaseManager
import logging

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME")
GEMINI_MODEL="gemini-2.5-pro"

# --- UI Styling Constants ---
GLASS_EFFECT = {
    "bgcolor": ft.Colors.with_opacity(0.1, ft.Colors.WHITE),
    "border": ft.border.all(1, ft.Colors.with_opacity(0.2, ft.Colors.WHITE)),
    "border_radius": ft.border_radius.all(15),
}


class Message(ft.Text):
    """A chat message control with a distinct style for user and AI."""

    def __init__(self, text: str, speaker: str):
        super().__init__()
        self.text = text
        self.speaker = speaker

    def build(self):
        is_user = self.speaker == "user"
        return ft.Row(
            controls=[
                ft.Container(
                    **GLASS_EFFECT,
                    padding=ft.padding.all(15),
                    margin=ft.margin.only(bottom=10, left=60 if not is_user else 0, right=60 if is_user else 0),
                    content=ft.Text(self.text, size=15, color=ft.Colors.WHITE),
                    expand=True,
                )
            ],
            alignment=ft.MainAxisAlignment.END if is_user else ft.MainAxisAlignment.START
        )


class ChatApp:
    def __init__(self, page: ft.Page, agent: SystemDesignAgent, db_manager: DatabaseManager):
        self.page = page
        self.agent = agent
        self.db_manager = db_manager
        self.current_discussion_id = None

        self._setup_page()
        self._build_layout()
        self.load_history_sidebar()

    def _setup_page(self):
        """Configures the main Flet page."""
        self.page.title = "System Design Co-Pilot"
        self.page.horizontal_alignment = ft.CrossAxisAlignment.STRETCH
        self.page.vertical_alignment = ft.MainAxisAlignment.START
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.padding = 0

    def _build_layout(self):
        """Constructs the main UI layout."""
        self.chat_view = ft.ListView(expand=True, auto_scroll=True, spacing=10)
        self.user_input = ft.TextField(
            hint_text="Type your message or a command like [next]...",
            on_submit=self.on_send_message,
            border_color=ft.Colors.with_opacity(0.3, ft.Colors.WHITE),
            border_radius=ft.border_radius.all(10),
            color=ft.Colors.WHITE
        )
        self.send_button = ft.IconButton(
            icon=ft.Icons.SEND_ROUNDED,
            on_click=self.on_send_message,
            tooltip="Send Message",
            icon_color=ft.Colors.WHITE
        )
        self.new_chat_button = ft.ElevatedButton(
            "New Discussion",
            icon=ft.Icons.ADD_COMMENT_ROUNDED,
            on_click=self.on_new_discussion,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=8),
                bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.WHITE)
            )
        )
        self.history_view = ft.ListView(expand=True, spacing=5)

        sidebar = ft.Container(
            **GLASS_EFFECT,
            width=250,
            padding=10,
            content=ft.Column([
                ft.Text("Past Discussions", size=18, weight=ft.FontWeight.BOLD),
                ft.Divider(height=1, color=ft.Colors.with_opacity(0.2, ft.Colors.WHITE)),
                self.new_chat_button,
                ft.Divider(height=1, color=ft.Colors.with_opacity(0.2, ft.Colors.WHITE)),
                self.history_view
            ])
        )

        main_chat_area = ft.Column(
            expand=True,
            controls=[
                self.chat_view,
                ft.Row([self.user_input, self.send_button], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
            ]
        )

        main_content = ft.Container(
            padding=20,
            expand=True,
            content=ft.Row([sidebar, ft.VerticalDivider(width=1), main_chat_area], expand=True)
        )

        background = ft.Container(
            gradient=ft.LinearGradient(
                begin=ft.alignment.top_center,
                end=ft.alignment.bottom_center,
                colors=[ft.Colors.INDIGO_900, ft.Colors.BLACK]
            ),
            expand=True,
            content=main_content
        )

        self.page.add(background)

    def on_send_message(self, e):
        """Handles the user sending a message."""
        user_text = self.user_input.value
        if not user_text:
            return

        self.user_input.value = ""
        self.user_input.disabled = True
        self.send_button.disabled = True

        # Add user message to UI immediately
        self.chat_view.controls.append(Message(user_text, "user"))
        self.page.update()

        # Start agent processing in a separate thread
        threading.Thread(target=self.run_agent_thread, args=(user_text,)).start()

    def run_agent_thread(self, user_text: str):
        """The background thread that runs the agent and updates the UI."""
        ai_message_control = Message("", "ai")
        self.chat_view.controls.append(ai_message_control)

        try:
            full_response = ""
            for chunk in self.agent.run_agent_stream(user_text, self.current_discussion_id):
                if "error" in chunk:
                    self.show_error(chunk["error"])
                    break

                # The first chunk contains the full new state, not message content
                if list(chunk.keys())[0] in self.agent.phases + ["summarize"]:
                    latest_step = list(chunk.values())[0]
                    # Update current discussion ID if it's a new chat
                    if not self.current_discussion_id:
                        self.current_discussion_id = latest_step.get("discussion_id")
                        self.load_history_sidebar()  # Refresh sidebar with new item

                    # Extract the last AI message from the history
                    new_content = latest_step.get("conversation_history", [])[-1][1]

                    # Update the AI message control
                    full_response = new_content
                    ai_message_control.value = full_response
                    self.page.update()

        except Exception as ex:
            logging.error(f"A critical error occurred in the agent thread: {ex}", exc_info=True)
            self.show_error(f"A critical error occurred: {ex}")
        finally:
            # Re-enable input fields on the main thread
            self.user_input.disabled = False
            self.send_button.disabled = False
            self.page.update()

    def show_error(self, message: str):
        """Displays a non-blocking SnackBar with an error message."""
        self.page.snack_bar = ft.SnackBar(
            content=ft.Text(message, color=ft.Colors.WHITE),
            bgcolor=ft.Colors.RED_700
        )
        self.page.snack_bar.open = True
        self.page.update()

    def load_history_sidebar(self):
        """Fetches past discussions and populates the sidebar."""
        self.history_view.controls.clear()
        discussions = self.db_manager.get_all_discussions()
        if discussions:
            for disc_id, title in discussions:
                self.history_view.controls.append(
                    ft.TextButton(
                        text=title,
                        data=disc_id,
                        on_click=self.on_history_item_selected,
                        style=ft.ButtonStyle(color=ft.Colors.WHITE70)
                    )
                )
        self.page.update()

    def on_history_item_selected(self, e):
        """Loads a past discussion into the main chat view."""
        selected_id = e.control.data
        self.current_discussion_id = selected_id

        self.chat_view.controls.clear()
        self.chat_view.controls.append(Message(f"Loading discussion: {selected_id[:8]}...", "ai"))
        self.page.update()

        discussion_state = self.db_manager.load_discussion(selected_id)

        self.chat_view.controls.clear()
        if discussion_state and "conversation_history" in discussion_state:
            for speaker, text in discussion_state["conversation_history"]:
                self.chat_view.controls.append(Message(text, speaker))
        else:
            self.show_error(f"Could not load discussion {selected_id}.")

        self.page.update()

    def on_new_discussion(self, e):
        """Resets the state to start a new conversation."""
        self.current_discussion_id = None
        self.chat_view.controls.clear()
        self.chat_view.controls.append(Message("New discussion started. What's on your mind?", "ai"))
        self.page.update()


def main(page: ft.Page):
    """The main entry point for the Flet application."""
    if not all([GOOGLE_API_KEY, MONGO_URI, MONGO_DB_NAME]):
        page.add(ft.Text("Error: Missing necessary environment variables (GOOGLE_API_KEY, MONGO_URI, MONGO_DB_NAME).", color=ft.Colors.RED))
        return

    try:
        db_manager = DatabaseManager(uri=MONGO_URI, db_name=MONGO_DB_NAME)
        llm = ChatGoogleGenerativeAI(model=GEMINI_MODEL, google_api_key=GOOGLE_API_KEY, temperature=0.7)
        agent = SystemDesignAgent(llm=llm, db_manager=db_manager)
        ChatApp(page, agent, db_manager)
    except Exception as e:
        logging.critical(f"Failed to initialize application: {e}", exc_info=True)
        page.add(ft.Text(f"Fatal Error: Could not start the application. Check logs. Error: {e}", color=ft.Colors.RED))


if __name__ == "__main__":
    ft.app(target=main)