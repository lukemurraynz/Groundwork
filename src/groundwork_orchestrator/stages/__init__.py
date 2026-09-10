"""Real execution stage implementations (T076-T083).

Each module here implements :class:`groundwork_orchestrator.engine.sequencer.Stage` for exactly
one ``blueprint.yaml`` stage name. ``Sequencer.__init__`` refuses to construct if any declared
blueprint stage has no entry in the registry it is given — these modules are that registry's
contents, wired together wherever the real ``Sequencer`` is constructed (the queue-consumption
loop, not yet added to ``groundwork_orchestrator.worker``).
"""
