/* Generated from demo.cur */
#include <stdio.h>

struct Account {
    int id;
    float balance;
};

int main(void) {
    printf("── Currency-C run ──\n");
    float price = 500.00f;
    float total;
    int count = 3;
    struct Account acc;
    struct Account *p = &acc;
    acc.id = 1;
    acc.balance = price * 2;
    p->balance = p->balance + 250.75f;
    total = acc.balance + 250.75f;
    printf("total = ₹%.2f\n", total);
    printf("p->balance = ₹%.2f\n", p->balance);
    printf("\n");
    while (count > 0) {
        printf(" count = %d\n", count);
        count = count - 1;
    }
    return 0;
}
