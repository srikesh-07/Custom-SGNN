import argparse
import random
from torch import tensor
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from numpy.core.numeric import False_
from subgraph_sample import generate_subgraph_samples
from categorizer import GraphCategorizer
from gin import GIN
from PatternMemory import PatternMemory
from util import k_fold, load_data, load_sample

criterion_ce = nn.CrossEntropyLoss()
criterion_cs = nn.CosineSimilarity(dim=1, eps=1e-7)


def train_gin(args, model, device, graphs, optimizer, epoch):
    model.train()
    print('epoch: %d' % (epoch), end=" ")
    loss_accum = 0
    minibatch_size = args.batch_size
    idx = np.arange(len(graphs))
    shuffle_idx = np.random.permutation(len(graphs))
    train_graphs = [graphs[id] for id in shuffle_idx]
    for i in range(0, len(train_graphs), minibatch_size):
        selected_idx = idx[i:i + minibatch_size]
        if len(selected_idx) == 0:
            continue
        batch_graph_h = [train_graphs[idx] for idx in selected_idx]
        output_h = model(batch_graph_h)
        labels_h = torch.LongTensor([graph.label for graph in batch_graph_h]).to(device)
        loss = criterion_ce(output_h, labels_h)
        if optimizer is not None:
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        loss = loss.detach().cpu().numpy()
        loss_accum += loss
    average_loss = loss_accum / float(len(train_graphs))
    print("loss training: %f" % (average_loss), end=' ')
    return average_loss


@torch.no_grad()
def pass_data_iteratively_gin(model, graphs, minibatch_size=128):
    model.eval()
    output = []
    idx = np.arange(len(graphs))
    for i in range(0, len(graphs), minibatch_size):
        sampled_idx = idx[i:i + minibatch_size]
        if len(sampled_idx) == 0:
            continue
        output.append(model([graphs[j] for j in sampled_idx]).detach())
    return torch.cat(output, 0)


@torch.no_grad()
def test_gin(args, model, device, graphs, epoch):
    model.eval()
    output = pass_data_iteratively_gin(model, graphs)
    pred = output.max(1, keepdim=True)[1]
    labels = torch.LongTensor(
        [graph.label for graph in graphs]).to(device)
    loss = criterion_ce(output, labels)
    correct = pred.eq(labels.view_as(
        pred)).sum().cpu().item()
    acc = correct / len(graphs)
    mask_h = torch.zeros(len(graphs))
    for j in range(len(graphs)):
        mask_h[j] = graphs[j].nodegroup
    mask_h = mask_h.bool()
    correct = pred[mask_h].eq(labels[mask_h].view_as(
        pred[mask_h])).sum().cpu().item()

    return loss, acc, correct


def train(args, model, patmem, device, graphs, samples, optimizer, optimizer_p, epoch):
    model.train()
    patmem.train()
    minibatch_size = args.batch_size
    loss_accum = 0
    print('epoch: %d' % (epoch + 1), end=" ")

    shuffle = np.random.permutation(len(graphs))
    train_graphs = [graphs[ind] for ind in shuffle]
    train_samples = [samples[ind] for ind in shuffle]

    idx = np.arange(len(train_graphs))

    l_h = l_t = l_n = l_g = l_d = 0

    for i in range(0, len(train_graphs), minibatch_size):

        selected_idx = idx[i:i + minibatch_size]

        if len(selected_idx) == 0:
            continue

        batch_graph_h = [train_graphs[idx]
                         for idx in selected_idx if train_graphs[idx].nodegroup == 1]
        batch_samples_h = [train_samples[idx]
                           for idx in selected_idx if train_graphs[idx].nodegroup == 1]
        batch_graph_t = [train_graphs[idx]
                         for idx in selected_idx if train_graphs[idx].nodegroup == 0]

        n_h = len(batch_graph_h)
        n_t = len(batch_graph_t)
        n = n_h + n_t

        if n_h <= 1 or n_t == 0:
            continue

        embeddings_head = model.get_patterns(batch_graph_h)

        gsize = np.zeros(n_h + 1, dtype=int)

        for i, graph in enumerate(batch_graph_h):
            gsize[i + 1] = gsize[i] + graph.g.number_of_nodes()

        q_idx = []
        pos_idx = []
        neg_idx = []
        pos_rep = []
        neg_rep = []

        for i, graph in enumerate(batch_graph_h):
            graph.sample_list = batch_samples_h[i].sample_list[epoch]
            gsize[i + 1] = gsize[i] + graph.g.number_of_nodes()
            uidx = batch_samples_h[i].unsample_list[epoch] + gsize[i]
            pos_rep.append(embeddings_head[uidx].sum(dim=0, keepdim=True))
            for _ in range(args.n_g):
                neg = np.random.randint(n_h)
                while (neg == i):
                    neg = np.random.randint(n_h)
                m = min(len(uidx), batch_graph_h[neg].g.number_of_nodes())
                sample_idx = torch.tensor(np.random.permutation(
                    batch_graph_h[neg].g.number_of_nodes())).long()
                sample_idx += gsize[neg]
                neg_rep.append(embeddings_head[sample_idx[:m]].sum(dim=0, keepdim=True))
            for _ in range(args.n_n):
                neg = np.random.randint(n_h)
                while (neg == i):
                    neg = np.random.randint(n_h)
                size = min(batch_graph_h[neg].g.number_of_nodes(
                ), batch_graph_h[i].g.number_of_nodes())
                q_idx.append(torch.arange(gsize[i], gsize[i] + size).long())
                sample_idx = torch.tensor(np.random.permutation(
                    graph.g.number_of_nodes())).long()
                sample_idx += gsize[i]
                pos_idx.append(sample_idx[:size])
                sample_idx = torch.tensor(np.random.permutation(
                    batch_graph_h[neg].g.number_of_nodes())).long()
                sample_idx += gsize[neg]
                neg_idx.append(sample_idx[:size])

        q_idx = torch.cat(q_idx).long()
        pos_idx = torch.cat(pos_idx).long()
        neg_idx = torch.cat(neg_idx).long()

        query = patmem(embeddings_head[q_idx])
        pos = embeddings_head[pos_idx]
        neg = embeddings_head[neg_idx]

        loss_n = - (torch.mul(query.div(torch.norm(query, dim=1).reshape(-1, 1) + 1e-7),
                              pos.div(torch.norm(pos, dim=1).reshape(-1, 1) + 1e-7)).sum(dim=1) -
                    torch.mul(query.div(torch.norm(query, dim=1).reshape(-1, 1) + 1e-7),
                              neg.div(torch.norm(neg, dim=1).reshape(-1, 1) + 1e-7)).sum(dim=1)).sigmoid().log().mean()

        subgraph_rep = model.subgraph_rep(batch_graph_h)
        pos_rep = torch.cat(pos_rep)
        neg_rep = torch.cat(neg_rep)
        query_g = patmem(subgraph_rep).repeat(args.n_g, 1)
        pos_g = pos_rep.repeat(args.n_g, 1)
        neg_g = neg_rep

        loss_g = - (torch.mul(query_g.div(torch.norm(query_g, dim=1).reshape(-1, 1) + 1e-7),
                              pos_g.div(torch.norm(pos_g, dim=1).reshape(-1, 1) + 1e-7)).sum(dim=1) -
                    torch.mul(query_g.div(torch.norm(query_g, dim=1).reshape(-1, 1) + 1e-7),
                              neg_g.div(torch.norm(neg_g, dim=1).reshape(-1, 1) + 1e-7)).sum(
                        dim=1)).sigmoid().log().mean()

        graph_repre_head = model.get_graph_repre(batch_graph_h)
        patterns_head = patmem(graph_repre_head)
        output_h = model.predict(graph_repre_head + patterns_head)
        labels_h = torch.LongTensor([graph.label for graph in batch_graph_h]).to(device)
        loss_h = criterion_ce(output_h, labels_h)

        graph_repre_tail = model.get_graph_repre(batch_graph_t)
        patterns_tail = patmem(graph_repre_tail)
        output_t = model.predict(graph_repre_tail + patterns_tail)
        labels_t = torch.LongTensor([graph.label for graph in batch_graph_t]).to(device)
        loss_t = criterion_ce(output_t, labels_t)

        loss_d = (criterion_cs(graph_repre_tail, patterns_tail).sum() + criterion_cs(graph_repre_head,
                                                                                     patterns_head).sum()) / n

        l_t += loss_t.detach().cpu().numpy()
        l_h += loss_h.detach().cpu().numpy()
        l_n += loss_n.detach().cpu().numpy()
        l_g += loss_g.detach().cpu().numpy()
        l_d += loss_d.detach().cpu().numpy()

        loss = 2 * (args.alpha * loss_h + (
                    1 - args.alpha) * loss_t) + args.mu1 * loss_n + args.mu2 * loss_g + args.lbd * loss_d

        optimizer.zero_grad()
        optimizer_p.zero_grad()
        loss.backward()
        optimizer.step()
        optimizer_p.step()

        loss = loss.detach().cpu().numpy()
        loss_accum += loss

    print("Loss Training: %f Head: %f Tail: %f Node: %f Graph: %f Dis: %f" %
          (loss_accum, l_t, l_h, l_n, l_g, l_d))

    return loss_accum


@torch.no_grad()
def pass_data_iteratively(args, model, patmem, graphs, device, minibatch_size=128):
    model.eval()
    patmem.eval()
    output = []
    labels = []
    idx = np.arange(len(graphs))
    for i in range(0, len(graphs), minibatch_size):
        selected_idx = idx[i:i + minibatch_size]
        if len(selected_idx) == 0:
            continue
        batch_graph = [graphs[i] for i in selected_idx]

        embeddings_graph = model.get_graph_repre(batch_graph)
        patterns = patmem(embeddings_graph)

        output.append(model.predict(embeddings_graph + patterns))
        labels.append(torch.LongTensor([graph.label for graph in batch_graph]))

    return torch.cat(output, 0), torch.cat(labels, 0).to(device)


@torch.no_grad()
def test(args, model, patmem, device, graphs, epoch):
    model.eval()
    output, labels = pass_data_iteratively(
        args, model, patmem, graphs, device)
    pred = output.max(1, keepdim=True)[1]
    labels = torch.LongTensor(
        [graph.label for graph in graphs]).to(device)
    loss_all = criterion_ce(output, labels)
    correct_all = pred.eq(labels.view_as(
        pred)).sum().cpu().item()
    acc_all = correct_all / len(graphs)
    mask = torch.zeros(len(graphs))
    for j in range(len(graphs)):
        mask[j] = graphs[j].graphgroup
    mask_head = (mask == 0)
    mask_medium = (mask == 1)
    mask_tail = (mask == 2)
    # loss_head = criterion_ce(output[mask_head], labels[mask_head])
    correct_head = pred[mask_head].eq(labels[mask_head].view_as(
        pred[mask_head])).sum().cpu().item()
    acc_head = correct_head / float(mask_head.sum())
    # loss_medium = criterion_ce(output[mask_medium], labels[mask_medium])
    correct_medium = pred[mask_medium].eq(labels[mask_medium].view_as(
        pred[mask_medium])).sum().cpu().item()
    acc_medium = correct_medium / float(mask_medium.sum())
    # loss_tail = criterion_ce(output[mask_tail], labels[mask_tail])
    correct_tail = pred[mask_tail].eq(labels[mask_tail].view_as(
        pred[mask_tail])).sum().cpu().item()
    acc_tail = correct_tail / float(mask_tail.sum())

    return loss_all, acc_all, acc_head, acc_medium, acc_tail


def main():
    # Note: Hyper-parameters need to be tuned in order to obtain results reported in the paper.
    parser = argparse.ArgumentParser(
        description='PyTorch graph convolutional neural net for whole-graph classification')
    parser.add_argument('--dataset', type=str, default="PTC",
                        help='name of dataset (default: PTC)')
    parser.add_argument('--size_strat', action='store_true')
    parser.add_argument('--device', type=int, default=0,
                        help='which g pu to use if any (default: 0)')
    parser.add_argument('--batch_size', type=int, default=32,
                        help='input batch size for training (default: 32)')
    parser.add_argument('--epochs', type=int, default=500,
                        help='number of epochs to train (default: 500)')
    parser.add_argument('--lr', type=float, default=0.01,
                        help='learning rate (default: 0.01)')
    parser.add_argument('--seed', type=int, default=0,
                        help='random seed for splitting the dataset')
    parser.add_argument('--test_ratio', type=float, default=0.2,
                        help='test data ratio')
    parser.add_argument('--valid_ratio', type=float, default=0.1,
                        help='valid data ratio')
    parser.add_argument('--num_layers', type=int, default=5,
                        help='number of layers INCLUDING the input one (default: 5)')
    parser.add_argument('--num_mlp_layers', type=int, default=2,
                        help='number of layers for MLP EXCLUDING the input one (default: 2). 1 means linear model.')
    parser.add_argument('--hidden_dim', type=int, default=32,
                        help='number of hidden units (default: 32)')
    parser.add_argument('--dropout', type=float, default=0.5,
                        help='final layer dropout (default: 0.5)')
    parser.add_argument('--graph_pooling_type', type=str, default="sum", choices=["sum", "average"],
                        help='Pooling for over nodes in a graph: sum or average')
    parser.add_argument('--neighbor_pooling_type', type=str, default="sum", choices=["sum", "average", "max"],
                        help='Pooling for over neighboring nodes: sum, average or max')
    parser.add_argument('--learn_eps', action="store_true",
                        help='Whether to learn the epsilon weighting for the center nodes. Does not affect training accuracy though.')
    parser.add_argument('--degree_as_tag', action="store_true",
                        help='let the input node features be the degree of nodes (heuristics for unlabeled graph)')
    parser.add_argument('--l2', type=float, default=5e-4,
                        help='the weight decay of adam optimizer')
    parser.add_argument('--alpha', type=float, default=0.5,
                        help=r'weight of head graph classification loss($\alpha $ in the paper)')
    parser.add_argument('--mu1', type=float, default=1.0,
                        help='weight of node-level co-occurrence loss($\mu_1$ in the paper)')
    parser.add_argument('--mu2', type=float, default=1.0,
                        help='weight of subgraph-level co-occurrence loss($\mu_2$ in the paper)')
    parser.add_argument('--lbd', type=float, default=1e-4,
                        help='weight of dissimilarity regularization loss($\lambda $ in the paper)')
    parser.add_argument('--dm', type=int, default=64,
                        help='dimension of pattern memory($d_m $ in the paper)')
    parser.add_argument('--K', type=int, default=72,
                        help='the number of head graphs($K $ in the paper)')
    parser.add_argument('--n_n', type=int, default=1,
                        help='the number of node-level co-occurrence triplets per node at single epoch')
    parser.add_argument('--n_g', type=int, default=1,
                        help='the number of subgraph-level co-occurrence triplets per graph at single epoch')
    parser.add_argument('--k_ratio', type=float, default=0.20,
                        help='[CUSTOM] Ratio of Graphs to Determine the K Ratio')
    parser.add_argument('--force_sampling', action="store_true",
                        help='[CUSTOM] Re-Intitate the Samplng')
    args = parser.parse_args()

    degree_state = 0
    seed = 0

    # Base Hyper-parameter configuration
    if args.dataset == "PROTEINS":
        hidden_dim = 32
        batch_size = 32
        seed = 2022
        learn_eps = False
        l2 = 0
    elif args.dataset == "PTC":
        hidden_dim = 32
        batch_size = 32
        seed = 0
        learn_eps = True
        l2 = 5e-4
    elif args.dataset == "IMDBBINARY":
        hidden_dim = 64
        batch_size = 32
        seed = 2020
        learn_eps = True
        l2 = 5e-4
    elif args.dataset == "DD":
        hidden_dim = 32
        batch_size = 128
        seed = 2022
        learn_eps = False
        l2 = 0
    elif args.dataset == "FRANK":
        hidden_dim = 32
        batch_size = 128
        learn_eps = True
        l2 = 5e-4
    else:
        hidden_dim = 32
        batch_size = 32
        learn_eps = True
        l2 = 5e-4

    args.hidden_dim = hidden_dim
    args.batch_size = batch_size
    args.l2 = l2
    args.learn_eps = learn_eps
    args.seed = seed

    graphs, num_classes = load_data(args.dataset, degree_state)
    args.K = int(len(graphs) * args.k_ratio)
    print(f'[INFO] Overwriting K as {args.K}')
    border = generate_subgraph_samples(dataset=args.dataset,
                                       k=args.K,
                                       force_sampling=args.force_sampling)
    gsamples = load_sample(args.dataset)

    nodes = list()

    for i in range(len(graphs)):
        nodes.append(graphs[i].g.number_of_nodes())

    _, ind = torch.sort(torch.tensor(nodes, dtype=torch.long), descending=True)

    for i in ind[:args.K]:
        graphs[i].nodegroup += 1

    # for i in ind[K[0]:K[1]]:
    #     graphs[i].graphgroup = 2
    # for i in ind[K[1]:K[2]]:
    #     graphs[i].graphgroup = 1
    # for i in ind[K[2]:K[3]]:
    #     graphs[i].graphgroup = 0
    split = GraphCategorizer(nodes=nodes)
    print("NUM HEAD GRAPHS: ", split.num_head_graphs)
    print("NUM MED GRAPHS: ", split.num_med_graphs)
    print("NUM TAIL GRAPHS: ", split.num_tail_graphs)
    print("SIZE RANGES: ", split.size_ranges)

    categories = GraphCategorizer(nodes).categories

    for i in range(len(graphs)):
        graphs[i].graphgroup = categories[i]

    if args.size_strat:
        print('[INFO] Using Strat Split')
        y = categories
    else:
        y = [graph.label for graph in graphs]

    folds = 5
    # train_indices, test_indices, val_indices = k_fold(graphs, folds=folds, y=categories)

    # test_record = torch.zeros(folds)
    # valid_record = torch.zeros(folds)
    # tail_record = torch.zeros(folds)
    # medium_record = torch.zeros(folds)
    # head_record = torch.zeros(folds)

    val_losses, accs, head_accs, med_accs, tail_accs, durations = [], [], [], [], [], []

    for fold, (train_idx, test_idx,
               val_idx) in enumerate(zip(*k_fold(graphs, folds, y))):

        train_graphs = [graphs[i] for i in train_idx]
        train_samples = [gsamples[i] for i in train_idx]
        test_graphs = [graphs[i] for i in test_idx]
        valid_graphs = [graphs[i] for i in val_idx]

        cnt_node = torch.zeros(3)

        for i in range(len(test_graphs)):
            cnt_node[test_graphs[i].graphgroup] += 1

        print('The number of graphs in test set:', end=' ')
        print("Head: %f" % (cnt_node[0]), end=', ')
        print("Med: %f" % (cnt_node[1]), end=', ')
        print("Tail: %f" % (cnt_node[2]))

        # times = 5
        seed = 12345

        random.seed(seed)
        torch.manual_seed(seed)
        np.random.seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
            torch.backends.cudnn.deterministic = True

        device = torch.device("cuda:" + str(args.device)
                              ) if torch.cuda.is_available() else torch.device("cpu")

        print("Train GIN firstly for head graphs")

        model = GIN(args.num_layers, args.num_mlp_layers, train_graphs[0].node_features.shape[1], args.hidden_dim,
                    num_classes, args.dropout, args.learn_eps, args.graph_pooling_type, args.neighbor_pooling_type,
                    device).to(device)

        optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.l2)

        scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=50, gamma=0.5)

        # test_acc = 0
        best_valid_acc = 0
        best_valid_loss = 100000
        # correct = 0
        patience = 0

        for epoch in range(0, args.epochs):
            _ = train_gin(args, model, device, train_graphs, optimizer, epoch)

            scheduler.step()

            loss_valid, acc_valid, _ = test_gin(args, model, device, valid_graphs, epoch)

            print("valid loss: %.4f acc: %.4f" % (loss_valid, acc_valid))

            if loss_valid < best_valid_loss and acc_valid > best_valid_acc:
                best_valid_acc = acc_valid
                best_valid_loss = loss_valid
                patience = 0
                _, test_acc, correct = test_gin(args, model, device, test_graphs, epoch)
                print("test acc: %.4f" % test_acc)
            else:
                patience += 1

            # if patience == 100:
            #     break

        print("Train SOLTGIN for tail graphs")

        model = GIN(args.num_layers, args.num_mlp_layers, train_graphs[0].node_features.shape[1], args.hidden_dim,
                    num_classes,
                    args.dropout, args.learn_eps, args.graph_pooling_type, args.neighbor_pooling_type, device).to(
            device)
        patmem = PatternMemory(args.hidden_dim, args.dm).to(device)

        optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.l2)

        scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=50, gamma=0.5)

        opt_p = optim.Adam(patmem.parameters(), lr=args.lr, weight_decay=args.l2)

        # test_acc = 0
        # best_valid_acc = 0
        # best_valid_loss = 100000
        # patience = 0
        #
        # best_test_acc = 0
        # best_test_head = 0
        # best_test_med = 0
        # best_test_tail = 0
        # test_acc_list= []
        # test_acc_head_list = []
        # test_acc_medium_list = []
        # test_acc_tail_list = []

        for epoch in range(0, args.epochs):
            _ = train(args, model, patmem, device, train_graphs, train_samples, optimizer, opt_p, epoch)

            scheduler.step()

            loss_valid, _, _, _, _ = test(args, model, patmem, device,
                                          valid_graphs, epoch)

            _, test_acc, test_acc_head, test_acc_medium, test_acc_tail = test(args, model, patmem, device,
                                                                              test_graphs, epoch)

            print("valid loss: %.4f acc: %.4f" % (loss_valid, acc_valid))
            val_losses.append(loss_valid)
            accs.append(test_acc)
            head_accs.append(test_acc_head)
            med_accs.append(test_acc_medium)
            tail_accs.append(test_acc_tail)

            # if loss_valid < best_valid_loss and acc_valid > best_valid_acc:
            #     best_valid_acc = acc_valid
            #     best_valid_loss = loss_valid
            #     loss, test_acc, test_acc_head, test_acc_medium, test_acc_tail = test(args, model, patmem, device,
            #                                                                          test_graphs, epoch)
            #     if test_acc > best_test_acc:
            #         best_test_acc = test_acc
            #         best_test_head = test_acc_head
            #         best_test_med = test_acc_medium
            #         best_test_tail = test_acc_tail
            #     print("test acc: %.4f" % best_test_acc)
            #     print("test acc_head: %.4f" % best_test_head)
            #     print("test acc_medium: %.4f" % best_test_med)
            #     print("test acc_tail: %.4f" % best_test_tail)
            #     # test_acc_list.append(test_acc)
            #     # test_acc_head_list.append(test_acc_head)
            #     # test_acc_medium_list.append(test_acc_medium)
            #     # test_acc_tail_list.append(test_acc_tail)
            #     patience = 0
            #     # _, tail_acc, correct_tail = test(args, model, patmem, device, test_graphs, epoch)
            # else:
            #     patience += 1

            # if patience == 100:
            #     break

    loss, acc, duration = tensor(val_losses), tensor(accs), tensor(durations)
    head_acc, med_acc, tail_acc = tensor(head_accs), tensor(med_accs), tensor(tail_accs)
    loss, acc = loss.view(folds, args.epochs), acc.view(folds, args.epochs)
    head_acc, med_acc, tail_acc = head_acc.view(folds, args.epochs), med_acc.view(folds, args.epochs), tail_acc.view(
        folds,
        args.epochs)
    loss, argmin = loss.min(dim=1)
    acc = acc[torch.arange(folds, dtype=torch.long), argmin]
    head_acc = head_acc[torch.arange(folds, dtype=torch.long), argmin]
    med_acc = med_acc[torch.arange(folds, dtype=torch.long), argmin]
    tail_acc = tail_acc[torch.arange(folds, dtype=torch.long), argmin]

    # loss_mean = loss.mean().item()
    acc_mean = acc.mean().item()
    acc_std = acc.std().item()
    head_acc_mean = head_acc.mean().item()
    head_acc_std = head_acc.std().item()
    med_acc_mean = med_acc.mean().item()
    med_acc_std = med_acc.std().item()
    tail_acc_mean = tail_acc.mean().item()
    tail_acc_std = tail_acc.std().item()

    # print('Valid mean: %.4f, std: %.4f' %
    #       (valid_record.mean().item(), valid_record.std().item()))
    # print('Test mean: %.4f, std: %.4f' %
    #       (test_record.mean().item(), test_record.std().item()))
    # print('Head mean: %.4f, std: %.4f' %
    #       (head_record.mean().item(), head_record.std().item()))
    # print('Medium mean: %.4f, std: %.4f' %
    #       (medium_record.mean().item(), medium_record.std().item()))
    # print('Tail mean: %.4f, std: %.4f' %
    #       (tail_record.mean().item(), tail_record.std().item()))

    with open("metrics.txt", "a") as txt_file:
        txt_file.write(f"Dataset: {args.dataset}, \n"
                       f"K: {args.K}, \n"
                       f"Subgraph Sampling Border: {border}, \n"
                       f"Alpha: {args.alpha}, \n"
                       f"Mu1: {args.mu1}, \n"
                       f"Mu2: {args.mu2}, \n"
                       f"Test Mean: {round(acc_mean, 4)}, \n"
                       f"Std Test Mean: {round(acc_std, 4)}, \n"
                       f"Head Mean: {round(head_acc_mean, 4)}, \n"
                       f"Std Head Mean: {round(head_acc_std, 4)}, \n"
                       f"Medium Mean: {round(med_acc_mean, 4)}, \n"
                       f"Std Medium Mean: {round(med_acc_std, 4)}, \n"
                       f"Tail Mean: {round(tail_acc_mean, 4)}, \n"
                       f"Std Tail Mean: {round(tail_acc_std, 4)} \n\n"
                       )

    # with open("metrics.txt", "a") as txt_file:
    #     txt_file.write(f"Dataset: {args.dataset}, \n"
    #                    f"Alpha: {args.alpha}, \n"
    #                    f"Mu1: {args.mu1}, \n"
    #                    f"Mu2: {args.mu2}, \n"
    #                    f"Valid Mean: {round(valid_record.mean().item(), 4)}, \n"
    #                    f"Std Valid Mean: {round(valid_record.std().item(), 4)}, \n"
    #                    f"Test Mean: {round(test_record.mean().item(), 4)}, \n"
    #                    f"Std Test Mean: {round(test_record.std().item(), 4)}, \n"
    #                    f"Head Mean: {round(head_record.mean().item(), 4)}, \n"
    #                    f"Std Head Mean: {round(head_record.std().item(), 4)}, \n"
    #                    f"Medium Mean: {round(medium_record.mean().item(), 4)}, \n"
    #                    f"Std Medium Mean: {round(medium_record.std().item(), 4)}, \n"
    #                    f"Tail Mean: {round(tail_record.mean().item(), 4)}, \n"
    #                    f"Std Tail Mean: {round(tail_record.std().item(), 4)} \n\n"
    #                    )


if __name__ == '__main__':
    main()
