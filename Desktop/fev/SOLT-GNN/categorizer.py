class GraphCategorizer:
    def __init__(self, nodes: list):
        self.nodes = nodes
        self.org_nodes = self._categorize_node_sizes()
        self._split_graphs()

    def _categorize_node_sizes(self):
        org_nodes = dict()
        for size in self.nodes:
            if org_nodes.get(size, None) is None:
                org_nodes[size] = 1
            else:
                org_nodes[size] += 1
        return org_nodes

    def _get_max_min_count(self, sorted_set: list):
        min = sorted_set[0]
        max = sorted_set[-1]
        total_count = 0
        for size in sorted_set:
            total_count += self.org_nodes[size]
        return (min, max), total_count

    def _split_graphs(self):
        self.equal_splits = list()
        sorted_nodes = sorted(list(self.org_nodes.keys()), reverse=True)
        category_size = len(sorted_nodes) // 3

        self.equal_splits.append(self._get_max_min_count(sorted_nodes[0: category_size]))
        self.equal_splits.append(self._get_max_min_count(sorted_nodes[category_size: category_size * 2]))
        self.equal_splits.append(self._get_max_min_count(sorted_nodes[category_size * 2:]))

        # self.equal_splits.sort(key=lambda x: x[-1], reverse=True)
    @property
    def num_head_graphs(self):
      return self.equal_splits[0][-1]

    @property
    def num_med_graphs(self):
      return self.equal_splits[1][-1]

    @property
    def num_tail_graphs(self):
      return self.equal_splits[2][-1]

    @property
    def size_ranges(self):
        return dict(head=self.equal_splits[0][0],
                    med=self.equal_splits[1][0],
                    tail=self.equal_splits[2][0])

    @property
    def categories(self):
        categories = list()
        for node in self.nodes:
            if (node >= self.equal_splits[0][0][1]) and (node <= self.equal_splits[0][0][0]):
                categories.append(0) #Head
            elif (node >= self.equal_splits[1][0][1]) and (node <= self.equal_splits[1][0][0]):
                categories.append(1) #Med
            elif (node >= self.equal_splits[2][0][1]) and (node <= self.equal_splits[2][0][0]):
                categories.append(2) #Tail
            else:
                assert False
        return categories
