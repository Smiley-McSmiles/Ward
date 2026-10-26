-- Empty OMEMO messages only set up sessions.  When they were addressed to other devices, Ward used to show
-- them as empty "This message was not encrypted for this device" system messages.  They carry no content, so
-- remove the noise that is already in the history.
DELETE FROM messages_fts
WHERE rowid IN (
    SELECT id FROM messages
    WHERE kind = 'system'
      AND body = ''
      AND stanza_id IS NULL
      AND extra LIKE '%"decrypt_error": "This message was not encrypted for this device"%'
);

DELETE FROM messages
WHERE kind = 'system'
  AND body = ''
  AND stanza_id IS NULL
  AND extra LIKE '%"decrypt_error": "This message was not encrypted for this device"%';
