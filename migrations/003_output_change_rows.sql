-- The Excel row an edited figure was read from, so each edited row is listed on its own.
ALTER TABLE output_changes ADD COLUMN excel_row INTEGER;
