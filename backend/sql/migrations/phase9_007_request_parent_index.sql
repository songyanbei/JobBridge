ALTER TABLE recommendation_request
  ADD KEY idx_recommendation_request_parent (parent_request_id);
