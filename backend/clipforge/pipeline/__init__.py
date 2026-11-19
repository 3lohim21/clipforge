"""Job handlers: the glue between the queue, the database, storage and the media engines.

Every stage is idempotent (safe to retry) and finishes by updating its row and enqueueing
the next stage in ONE transaction, so a crash can never leave a video "between" stages.

    video.ingest -> video.transcribe (unless platform captions were found) -> video.analyze
    clip.render (on demand)      storage.cleanup / posts.refresh (maintenance)
"""
