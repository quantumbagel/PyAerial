import sys
import threading


class Datum:
    """
    Store a value/time pair.
    """
    def __init__(self, value, timestamp):
        self.value = value
        self.time = timestamp

    def __str__(self):
        return f"Datum({self.value}, {self.time})"

    def __repr__(self):
        return self.__str__()

    def __eq__(self, other):
        if other is None:
            return False
        return self.value == other.value and self.time == other.time


class ImprovedThread(threading.Thread):
    """
    A very similar version of threading.Thread that returns the value of the thread process
    with Thread.join()
    It also prints exceptions when they are thrown.

    Ripped from finiteCraft lmao
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize the ImprovedThread
        :param args:
        :param kwargs:
        """
        super().__init__(*args, **kwargs)
        self.result = None

    def run(self) -> None:
        """
        Run the ImprovedThread.
        :return:
        """
        if self._target is None:
            return  # could alternatively raise an exception, depends on the use case
        try:
            self.result = self._target(*self._args, **self._kwargs)
        except Exception as exc:
            print(f'{type(exc).__name__}: {exc}', file=sys.stderr)  # properly handle the exception
            raise exc

    def join(self, *args, **kwargs) -> dict:
        """
        The highlight of the class. Returns the thread result upon ending.
        :param args:
        :param kwargs:
        :return:
        """
        super().join(*args, **kwargs)
        return self.result
