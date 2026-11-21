-- Whether a part went as text or as an image; images count against Zalo's monthly upload quota.
ALTER TABLE output_sends ADD COLUMN style TEXT NOT NULL DEFAULT 'text';
