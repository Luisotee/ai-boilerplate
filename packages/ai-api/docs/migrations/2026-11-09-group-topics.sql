-- Group topics: a (user_id, timestamp, id) index on conversation_messages.
--
-- The group-topics job (rag/group_topics.py) reads each group's messages
-- after its cursor in (timestamp, id) order, and checks every group for such
-- messages on each run. Without this index both filter on user_id and then
-- sort or scan the group's whole history. The name and definition match what
-- create_all() emits on a fresh DB. The group_topics and group_topic_cursors
-- tables themselves need nothing here: create_all() creates new tables.
--
-- CONCURRENTLY keeps the bot saving messages while the index builds. It can't
-- run inside a transaction, so apply this file with plain `psql < file` (no
-- -1 / --single-transaction). Safe to re-run: an index left INVALID by an
-- interrupted build is dropped and rebuilt.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
        WHERE c.relname = 'idx_conversation_messages_user_ts_id' AND NOT i.indisvalid
    ) THEN
        DROP INDEX idx_conversation_messages_user_ts_id;
    END IF;
END $$;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conversation_messages_user_ts_id
    ON conversation_messages (user_id, timestamp, id);
