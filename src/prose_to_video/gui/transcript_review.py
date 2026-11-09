"""Listen, compare, and correct transcript text while leaving source prose intact."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
    QPlainTextEdit, QPushButton, QCheckBox, QSplitter, QWidget)
from prose_to_video.gui.audio_player import AudioPlayer
from prose_to_video.pipeline.transcript_review import ReviewDraft, segment_review


class TranscriptReviewDialog(QDialog):
    def __init__(self, session, run, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Review Narration'); self.resize(1150, 800)
        self.session=session; self.run=run
        self.draft=ReviewDraft.load(session.root,run)
        self.rows=segment_review(run,text); self.current=None; self.loading=False
        layout=QVBoxLayout(self)
        intro=QLabel('Listen to flagged passages and correct what the transcript says was spoken. '
            'Keep actual extra or repeated speech in the transcript. Leave manuscript text unchanged. '
            'Re-aligning obtains new audio timings and checks for omissions and extra speech before building.')
        intro.setWordWrap(True); layout.addWidget(intro)
        self.only_problems=QCheckBox('Show discrepancies only'); self.only_problems.setChecked(True)
        layout.addWidget(self.only_problems)
        split=QSplitter(); layout.addWidget(split,1)
        self.listing=QListWidget(); split.addWidget(self.listing)
        detail=QWidget(); form=QVBoxLayout(detail); split.addWidget(detail); split.setStretchFactor(1,3)
        self.problems=QLabel(); self.problems.setWordWrap(True); self.problems.setTextFormat(Qt.PlainText)
        form.addWidget(self.problems)
        form.addWidget(QLabel('Manuscript context (read only)'))
        self.manuscript=QPlainTextEdit(); self.manuscript.setReadOnly(True); form.addWidget(self.manuscript)
        form.addWidget(QLabel('Transcript — edit to match the recording'))
        self.transcript=QPlainTextEdit(); form.addWidget(self.transcript)
        self.listened=QCheckBox('I listened to this passage and checked the transcript'); form.addWidget(self.listened)
        self.play=QPushButton('Play selected passage'); form.addWidget(self.play)
        self.audio=AudioPlayer(); self.audio.set_audio(session.project.resolved_audio(session.root),run.audio_duration)
        layout.addWidget(self.audio)
        self.status=QLabel('Corrections are saved as a draft. New timings are published only after re-alignment succeeds.')
        self.status.setWordWrap(True); layout.addWidget(self.status)
        actions=QHBoxLayout(); layout.addLayout(actions)
        self.save=QPushButton('Save Draft'); self.realign=QPushButton('Re-align and Build Timeline'); close=QPushButton('Close')
        for button in (self.save,self.realign,close): actions.addWidget(button)
        self.save.clicked.connect(self.save_draft); self.realign.clicked.connect(self.accept_review); close.clicked.connect(self.reject)
        self.only_problems.toggled.connect(self.populate)
        self.listing.currentRowChanged.connect(self.select)
        self.transcript.textChanged.connect(self.edited)
        self.play.clicked.connect(self.play_selected)
        self.audio.playback_error.connect(self.status.setText)
        self.populate()

    def edited(self):
        if not self.loading: self.listened.setChecked(False)

    def store(self):
        if self.current is None: return
        identifier=self.run.transcript[self.current].identifier
        self.draft.texts[identifier]=self.transcript.toPlainText()
        self.draft.listened=[s for s in self.draft.listened if s!=identifier]
        if self.listened.isChecked(): self.draft.listened.append(identifier)

    def save_draft(self):
        self.store()
        try: self.draft.save(self.session.root)
        except OSError as exc:
            self.status.setText(f'Could not save transcript draft: {exc}'); return False
        self.status.setText('Transcript draft saved. Re-align to apply corrections and check narration.')
        return True

    def populate(self):
        self.store(); self.current=None
        self.listing.blockSignals(True); self.listing.clear()
        self.indexes=[i for i,row in enumerate(self.rows) if row['messages'] or not self.only_problems.isChecked()]
        for i in self.indexes:
            s=self.run.transcript[i]; count=len(self.rows[i]['messages'])
            self.listing.addItem(f'{s.start/60:05.2f} min · {count} discrepancies · {s.text[:45]}')
        self.listing.blockSignals(False)
        if self.indexes: self.listing.setCurrentRow(0)
        else:
            self.problems.setText('No discrepancies. Uncheck the filter to review any passage, or re-align and build.')
            self.manuscript.clear(); self.transcript.clear()
        for widget in (self.transcript,self.listened,self.play): widget.setEnabled(bool(self.indexes))

    def select(self,row):
        if row<0: return
        if self.current is not None and not self.save_draft(): return
        self.current=self.indexes[row]; segment=self.run.transcript[self.current]; info=self.rows[self.current]
        self.loading=True
        self.problems.setText('\n'.join(info['messages']) or 'No detected discrepancies in this passage.')
        self.manuscript.setPlainText(info['context'])
        self.transcript.setPlainText(self.draft.texts.get(segment.identifier,segment.text))
        self.listened.setChecked(segment.identifier in self.draft.listened)
        self.loading=False

    def play_selected(self):
        if self.current is None: return
        segment=self.run.transcript[self.current]
        self.audio.play_range(max(0,segment.start-.5),min(self.run.audio_duration,segment.end+.5))

    def accept_review(self):
        if not self.save_draft(): return
        edited=[s for s in self.run.transcript if self.draft.texts.get(s.identifier,s.text)!=s.text]
        if any(s.identifier not in self.draft.listened for s in edited):
            self.status.setText('Listen to every edited passage and mark its transcript checked before re-aligning.'); return
        self.audio.player.stop(); self.accept()

    def reject(self):
        if self.save_draft():
            self.audio.player.stop(); super().reject()
