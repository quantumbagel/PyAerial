import logging


class Saver:
    def __init__(self, lname: str = "Saver"):
        self.logger = logging.getLogger(name=lname)
        self._cache = {}

    def cache(self, plane_id: str, packet_category: str, packet_subcategory: str, packet_data: list[list] | list):
        multiple_packets = type(packet_data[0]) is list
        if self._cache.get(plane_id) is None:
            if multiple_packets: 
                self._cache[plane_id] = {packet_category: {packet_subcategory: packet_data}}
                # Create p_c, and p_sc
            else:  # only one packet
                self._cache[plane_id] = {packet_category: {packet_subcategory: [packet_data]}}

        elif self._cache[plane_id].get(packet_category) is None:
            if multiple_packets: 
                self._cache[plane_id][packet_category] = {packet_subcategory: packet_data}  # p_sc
            else: 
                self._cache[plane_id][packet_category] = {packet_subcategory: [packet_data]}

        elif self._cache[plane_id][packet_category].get(packet_subcategory) is None:
            if multiple_packets: 
                self._cache[plane_id][packet_category][packet_subcategory] = packet_data
            else:
                self._cache[plane_id][packet_category][packet_subcategory] = [packet_data]

        else:  # Already another packet in this datum
            if multiple_packets:
                self._cache[plane_id][packet_category][packet_subcategory].extend(packet_data)
            else:
                self._cache[plane_id][packet_category][packet_subcategory].append(packet_data)

    def save(self, prebuilt_cache: dict = None, dump_cache: bool = True):
        pass
    

class PrintSaver(Saver):
    def __init__(self):
        super().__init__(lname="PrintSaver")

    def save(self, prebuilt_cache: dict = None, dump_cache: bool = True):
        if prebuilt_cache is not None:
            self.logger.info(f"SAVING: {prebuilt_cache}")
        else:
            self.logger.info(f"SAVING: {self._cache}")
            if dump_cache:
                self._cache = {}


