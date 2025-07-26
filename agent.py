# agent.py
"""
The core agent logic implemented with LangGraph.
- Defines the state machine for the system design process.
- Manages the conversation flow through different phases.
- Streams responses and persists state at each step.
"""
import logging
from typing import TypedDict, List, Tuple, Annotated, Dict, Any, Optional
from bson.objectid import ObjectId

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, END

from database import DatabaseManager
from prompts import (
    SYSTEM_PERSONA, VISION_AND_SCOPING_PROMPT, FUNCTIONAL_REQUIREMENTS_PROMPT,
    DATA_MODEL_PROMPT, NFR_AND_SCALE_PROMPT, ARCHITECTURE_AND_COMPONENTS_PROMPT,
    DEEP_DIVE_AND_TRADEOFFS_PROMPT, SUMMARY_PROMPT, ROUTER_PROMPT
)


class AgentState(TypedDict):
    """Represents the state of our conversation graph."""
    discussion_id: str
    conversation_history: List[Tuple[str, str]]  # (speaker, text)
    current_phase: str
    design_document: Dict[str, str]
    user_command: str


class SystemDesignAgent:
    """The main class for the System Design Co-Pilot agent."""

    def __init__(self, llm, db_manager: DatabaseManager):
        """
        Initializes the agent, its tools, and the LangGraph.

        Args:
            llm: An initialized LangChain chat model (e.g., ChatGoogleGenerativeAI).
            db_manager (DatabaseManager): An instance of the database manager.
        """
        self.llm = llm
        self.db_manager = db_manager

        self.phases = [
            "vision_and_scoping",
            "functional_requirements",
            "data_model",
            "nfr_and_scale",
            "architecture_and_components",
            "deep_dive_and_tradeoffs"
        ]

        self.phase_prompts = {
            "vision_and_scoping": VISION_AND_SCOPING_PROMPT,
            "functional_requirements": FUNCTIONAL_REQUIREMENTS_PROMPT,
            "data_model": DATA_MODEL_PROMPT,
            "nfr_and_scale": NFR_AND_SCALE_PROMPT,
            "architecture_and_components": ARCHITECTURE_AND_COMPONENTS_PROMPT,
            "deep_dive_and_tradeoffs": DEEP_DIVE_AND_TRADEOFFS_PROMPT,
        }

        self.graph = self._create_graph()

    def _create_graph(self) -> StateGraph:
        """Builds the LangGraph state machine."""
        graph = StateGraph(AgentState)

        # Add nodes for each phase
        for phase_name in self.phases:
            graph.add_node(phase_name, self._create_phase_node(phase_name))
        graph.add_node("summarize", self._summary_node)

        # Define the entry point
        graph.set_entry_point(self.phases[0])

        # Add edges for the standard flow
        graph.add_conditional_edges(
            self.phases[0],
            self._router,
            {**{p: p for p in self.phases}, "summarize": "summarize", "end": END}
        )
        for i in range(1, len(self.phases)):
            graph.add_conditional_edges(
                self.phases[i],
                self._router,
                {**{p: p for p in self.phases}, "summarize": "summarize", "end": END}
            )

        # The summary node can only end the conversation
        graph.add_edge("summarize", END)

        return graph.compile()

    def _format_history(self, history: List[Tuple[str, str]]) -> List[BaseMessage]:
        """Formats the custom history tuple into LangChain messages."""
        messages = []
        for speaker, text in history:
            if speaker == "user":
                messages.append(HumanMessage(content=text))
            else:
                messages.append(AIMessage(content=text))
        return messages

    def _create_phase_node(self, phase_name: str):
        """A factory to create a node function for a given phase."""

        def phase_node(state: AgentState) -> Dict[str, Any]:
            self.db_manager.write_log(phase_name, {"discussion_id": state["discussion_id"]})

            # Start of a new phase, so ask the guiding question
            is_new_phase = state['conversation_history'][-1][0] == "user"

            prompt_messages = [
                HumanMessage(content=SYSTEM_PERSONA),
                *self._format_history(state["conversation_history"])
            ]
            if is_new_phase:
                prompt_messages.append(HumanMessage(content=self.phase_prompts[phase_name]))

            try:
                response = self.llm.invoke(prompt_messages)
                ai_message = response.content
            except Exception as e:
                logging.error(f"LLM call failed in node {phase_name}: {e}")
                ai_message = "I seem to be having trouble connecting. Could you try your message again?"

            # Update state
            updated_history = state["conversation_history"] + [("ai", ai_message)]
            current_doc = state.get("design_document", {})
            current_doc[phase_name] = current_doc.get(phase_name, "") + "\n" + "\n".join(
                [msg[1] for msg in state["conversation_history"][-1:]]) + f"\nAI: {ai_message}"

            return {
                "conversation_history": updated_history,
                "design_document": current_doc,
            }

        return phase_node

    def _router(self, state: AgentState) -> str:
        """Determines the next node to visit based on user command."""
        self.db_manager.write_log("router", {"command": state["user_command"]})
        command = state["user_command"].lower().strip()
        current_phase = state["current_phase"]

        if "[next]" in command:
            current_index = self.phases.index(current_phase)
            next_index = min(current_index + 1, len(self.phases) - 1)
            return self.phases[next_index]
        elif "[back]" in command:
            current_index = self.phases.index(current_phase)
            next_index = max(current_index - 1, 0)
            return self.phases[next_index]
        elif "[summarize]" in command:
            return "summarize"
        elif "[end]" in command or "[exit]" in command:
            return "end"
        else:
            # If no command, stay in the current phase for more discussion
            return current_phase

    def _summary_node(self, state: AgentState) -> Dict[str, Any]:
        """Generates and presents a summary of the design document."""
        self.db_manager.write_log("summary", {"discussion_id": state["discussion_id"]})

        full_document_text = ""
        for phase in self.phases:
            if phase in state["design_document"]:
                full_document_text += f"--- {phase.replace('_', ' ').title()} ---\n{state['design_document'][phase]}\n\n"

        try:
            prompt = ChatPromptTemplate.from_template(SUMMARY_PROMPT)
            chain = prompt | self.llm
            response = chain.invoke({"design_document": full_document_text})
            summary_message = response.content
        except Exception as e:
            logging.error(f"LLM call failed in summary node: {e}")
            summary_message = f"I encountered an error while generating the summary. Here is the raw data:\n\n{full_document_text}"

        return {"conversation_history": state["conversation_history"] + [("ai", summary_message)]}

    def run_agent_stream(self, user_input: str, discussion_id: Optional[str] = None):
        """
        Runs the agent, streaming the output.

        Args:
            user_input (str): The user's latest message.
            discussion_id (Optional[str]): The ID of an existing discussion to resume.

        Yields:
            Dict[str, Any]: Chunks of the streaming response from the graph.
        """
        try:
            # Step 1: Load or initialize state
            if discussion_id:
                current_state = self.db_manager.load_discussion(discussion_id)
                if not current_state:
                    yield {"error": f"Could not load discussion {discussion_id}."}
                    return
            else:
                new_id = str(ObjectId())
                current_state: AgentState = {
                    "discussion_id": new_id,
                    "conversation_history": [],
                    "current_phase": self.phases[0],
                    "design_document": {},
                    "user_command": ""
                }

            # Step 2: Update state with new user input
            current_state["user_command"] = user_input
            current_state["conversation_history"].append(("user", user_input))

            # Step 3: Stream the graph execution
            for chunk in self.graph.stream(current_state):
                # Yield the chunk for the UI to process
                yield chunk

                # After each step, persist the latest complete state
                latest_step_output = list(chunk.values())[0]
                current_state.update(latest_step_output)

                # Determine the *next* phase from the router for the state
                next_phase = self._router(current_state)
                if next_phase != 'end':
                    current_state["current_phase"] = next_phase

                self.db_manager.save_discussion(current_state)

        except Exception as e:
            logging.error(f"Critical error in agent stream: {e}", exc_info=True)
            yield {"error": f"An unexpected error occurred in the agent: {e}"}