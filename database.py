# database.py
"""
Handles all database interactions with MongoDB.
- Manages connection to the database.
- Provides CRUD operations for discussions and logs.
- Implements robust error handling for all database operations.
"""
import logging
from typing import Dict, Any, Optional, List, Tuple
import time
import pymongo
from bson.objectid import ObjectId
from pymongo.errors import ConnectionFailure, OperationFailure
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi

# Set up basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


class DatabaseManager:
    """A robust manager for MongoDB interactions."""

    def __init__(self, uri: str, db_name: str):
        """
        Initializes the database connection.

        Args:
            uri (str): The MongoDB connection string.
            db_name (str): The name of the database to use.
        """
        self.client = None
        self.db = None
        try:
            self.client = MongoClient(uri, server_api=ServerApi('1'))
            # Send a ping to confirm a successful connection
            self.client.admin.command('ping')
            self.db = self.client[db_name]
            logging.info("Successfully connected to MongoDB.")
            self._setup_collections()
        except ConnectionFailure as e:
            logging.error(f"Could not connect to MongoDB: {e}")
            raise
        except Exception as e:
            logging.error(f"An unexpected error occurred during DB initialization: {e}")
            raise

    def _setup_collections(self):
        """Ensures the required collections exist."""
        try:
            if "discussions" not in self.db.list_collection_names():
                self.db.create_collection("discussions")
                logging.info("Created 'discussions' collection.")
            if "logs" not in self.db.list_collection_names():
                self.db.create_collection("logs")
                logging.info("Created 'logs' collection.")
        except OperationFailure as e:
            logging.error(f"Failed to create collections: {e}")
            raise

    def write_log(self, node: str, data: Dict[str, Any]) -> bool:
        """
        Writes a log entry to the 'logs' collection.

        Args:
            node (str): The name of the graph node being executed.
            data (Dict[str, Any]): The data to log.

        Returns:
            bool: True if write was successful, False otherwise.
        """
        if self.db is None:
            logging.error("Database not connected. Cannot write log.")
            return False
        try:
            log_entry = {"node": node, "data": data, "timestamp": int(time.time())}
            self.db.logs.insert_one(log_entry)
            return True
        except OperationFailure as e:
            logging.error(f"Failed to write log to MongoDB: {e}")
            return False

    def save_discussion(self, state: Dict[str, Any]) -> bool:
        """
        Saves or updates a discussion state in the 'discussions' collection.

        Args:
            state (Dict[str, Any]): The agent's state dictionary.

        Returns:
            bool: True if save was successful, False otherwise.
        """
        if self.db is None:
            logging.error("Database not connected. Cannot save discussion.")
            return False
        try:
            discussion_id = ObjectId(state["discussion_id"])
            # Use upsert to create the document if it doesn't exist
            self.db.discussions.update_one(
                {"_id": discussion_id},
                {"$set": state},
                upsert=True
            )
            return True
        except OperationFailure as e:
            logging.error(f"Failed to save discussion {state.get('discussion_id')}: {e}")
            return False
        except Exception as e:
            logging.error(f"An unexpected error occurred while saving discussion: {e}")
            return False

    def load_discussion(self, discussion_id: str) -> Optional[Dict[str, Any]]:
        """
        Loads a discussion state from the database.

        Args:
            discussion_id (str): The unique ID of the discussion.

        Returns:
            Optional[Dict[str, Any]]: The state dictionary or None if not found or on error.
        """
        if self.db is None:
            logging.error("Database not connected. Cannot load discussion.")
            return None
        try:
            obj_id = ObjectId(discussion_id)
            state = self.db.discussions.find_one({"_id": obj_id})
            if state and '_id' in state:
                state['discussion_id'] = str(state['_id'])
                del state['_id']  # Clean up the mongo-specific key
            return state
        except OperationFailure as e:
            logging.error(f"Failed to load discussion {discussion_id}: {e}")
            return None
        except Exception as e:  # Catches invalid ObjectId format, etc.
            logging.error(f"An error occurred loading discussion {discussion_id}: {e}")
            return None

    def get_all_discussions(self) -> Optional[List[Tuple[str, str]]]:
        """
        Retrieves a list of all past discussions for the history view.
        Returns only the ID and a title derived from the first user message.

        Returns:
            Optional[List[Tuple[str, str]]]: A list of (id, title) tuples, or None on error.
        """
        if self.db is None:
            logging.error("Database not connected. Cannot get discussions.")
            return None
        try:
            discussions = []
            cursor = self.db.discussions.find(
                {},
                {"_id": 1, "conversation_history": 1}
            ).sort("timestamp", pymongo.DESCENDING)  # Assuming you add a timestamp

            for doc in cursor:
                doc_id = str(doc['_id'])
                # Find the first user message for a title
                title = "New Discussion"
                if doc.get("conversation_history"):
                    first_user_msg = next((msg for msg in doc["conversation_history"] if msg[0] == "user"), None)
                    if first_user_msg:
                        title = first_user_msg[1][:50] + "..."  # Truncate for display
                discussions.append((doc_id, title))
            return discussions
        except OperationFailure as e:
            logging.error(f"Failed to retrieve all discussions: {e}")
            return None
