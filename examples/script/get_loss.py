if __name__ == '__main__':
    cnt = 0
    with open('logs/eval_subnet_tq_rand_insuper_expand'
              '.blk8s1_diff3_chu3_sta40_con1e-3.txt') as rfid:
        for line in rfid:
            if 'Loss' in line:
                cnt += 1
                if cnt % 2:
                    print(eval(line.split(' ')[-1]))
