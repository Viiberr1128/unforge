"""Small event signal; consumers reread authoritative state when it changes."""
import threading

class ChangeFeed:
    def __init__(self):
        self.condition = threading.Condition()
        self.revision = 0
        self.closed = False

    def publish(self):
        with self.condition:
            self.revision += 1
            self.condition.notify_all()

    def wait(self, revision, timeout=25):
        with self.condition:
            self.condition.wait_for(lambda: self.closed or self.revision != revision, timeout)
            return self.revision, self.closed

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
